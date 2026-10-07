# Leveling / Dashboard Investigation — Findings & Architecture Review

**Scope:** read-only investigation of the Nilive-Bot repository at commit `ddb534a` (branch `arena/01a10d19-nilive-bot`).
No application code, template, test or config was modified. This report is the only file added.

> Historical snapshot only. Its code-state findings (including the former spam threshold/window defaults) are not current configuration. See `XP_PACING_SIMULATION.md` and the active `dashboard/templates/systems/leveling.html` for the current, tested Leveling defaults and behavior.

**Labels used throughout**

| Label | Meaning |
|---|---|
| **PROVEN (code)** | established by reading the current source, with `file:line` |
| **PROVEN (runtime)** | reproduced by executing the real code (Flask test client against the real app, real templates/JS under a DOM harness, real discord.py objects, real htmx 1.9.10) |
| **INFERRED** | consistent with all evidence, mechanism understood, but not directly executed end-to-end |
| **NEEDS A DECISION** | a product/architecture choice only you can make |

**How the runtime evidence was produced** (nothing in the repo was changed; all probes live outside it):

* Flask test client bound to the real `dashboard.app`, with a forged session (`user`, `guild_id`, future `expires_at`), against a scratch DB (`/tmp/neuro/nero.db`) — for route/API/HTML/status-code facts.
* Node + jsdom harness serving the **real rendered `/leveling` page** (saved from the live app), the real `dashboard.js` / `nero-select.js` / `nav-lifecycle.js`, and the real **htmx 1.9.10 / jQuery 3.7.1 / select2 4.1.0-rc.0** browser bundles, driven through real `htmx.ajax()` navigation (patch: jsdom's XHR has no `overrideMimeType`, which the htmx source calls).
* `node vm` cross-realm test for classic-script re-declaration semantics.
* A Python re-implementation of the live XP gate order with a **simulated clock** for the level-rate numbers in §5 (the live listeners use wall-clock `time.time()`; the listeners themselves were *not* executed).

**Known limits of the evidence:** no live Discord traffic was generated (no real `/level` click, no real message/voice tick), and the checkout contains **no database file**, so the *deployed* `leveling_config` row values cannot be read from here (see §3/§4). Where a conclusion depends on those two things it is labelled INFERRED or NEEDS A DECISION.

---

## 0. Executive summary

1. **The dashboard-wide "blank / shows up only on a later click or after refresh" problem has one dominant cause: every page's inline `<script>` block is emitted twice in the served HTML, and both copies are evaluated as classic scripts in the same JS realm.** A second evaluation of a script that declares a top-level `const`/`let` throws `SyntaxError` for the *whole* script, so on htmx navigation the page's JavaScript never runs (from the second visit to that page onward). It is a **frontend lifecycle/script-realm bug — not an API, backend-exception, DB or race bug** (those exist, but separately; see §2).
2. **"Save Config → Connection error" is a response-format bug:** `POST /api/leveling/config` converts 14 form values with bare `int()`. A blank or decimal field (`""`, `"30.5"`) raises `ValueError` → Flask's HTML 500 page → `ajaxSave`'s `res.json()` throws → its catch shows `Connection error`. Nothing is saved in that case. A separate class (failure *after* the commit) returns 500 **and persists the row** — that is the real "saved anyway".
3. **The Leveling master toggle is effectively ON by default** in the bot (fallback `enabled: 1`), but **on a guild with no `leveling_config` row the dashboard renders it OFF with every numeric field blank**, because the API returns `{"config": {}}` and the page writes `undefined` into the form. That both misrepresents the bot's behaviour and makes an untouched "Save" fail with the error from item 2.
4. **The Level-100-in-10-months target is not reachable with current defaults:** at 6 h/day of chat + voice the ceiling is ~6 840 XP/day → **Level 76 after 300 days**; Level 100 needs ~13 500 XP/day → **≈592–703 days**. The binding constraint is `xp_cooldown_seconds = 30` (120 XP-messages/hour), not the XP curve.
5. **Voice XP cannot be earned alone — that is a hard-coded gate in the Activity Engine** (`len(real_members) < 2: continue`), *not* a setting. The desired independent "require another participant" toggle cannot be implemented in the Leveling listener alone: when the member is alone the tick is never dispatched, and the same tick also feeds Missions and MVP scores.
6. `/resetleaderboard` is transactional and does not touch entitlements — but it **silently arms the recurring weekly/monthly auto-reset** (`leveling_reset_config.enabled = 1` on a new row), which is the most consequential behaviour in the command.
7. Two settings are wired-only at runtime: `remove_old_reward_role` (its only reader is dead code) and `spam_window_seconds` (read at runtime, but no dashboard field/API write). `levelup_embed_data` is a fully dead column.

---

## 1. Dashboard-wide root causes of loading / partial-render / "content on second click" / needs-refresh

### 1.1 The dominant cause, PROVEN: the page script is emitted twice and re-evaluated in one realm

**Step 1 — the duplication is produced by the template structure (PROVEN code + PROVEN runtime).**
Every affected page template declares `{% block scripts %}` **inside** `{% block content %}` (and then closes `content` after it). Jinja renders a child's block override *both* where the child puts it *and* where the parent declares it (`base.html:593`). Therefore the whole page `<script>` block appears **twice** in the served HTML.

23 page templates have that shape. Verified in the served HTML (real Flask app, forged session) — identical inline blocks, exact byte sizes:

| Route | duplicated inline block | duplicated function bodies |
|---|---|---|
| `/leveling` | 21 887 B ×2 | 30 |
| `/minigames` | 27 982 B ×2 | 42 |
| `/commands` | 27 345 B ×2 | 17 |
| `/missions` | 10 474 B ×2 | 13 |
| `/shop` | 8 729 B ×2 | 5 |
| `/economy` | 6 432 B ×2 | 7 |
| `/ledger` | 3 276 B ×2 | 2 |
| `/trade` | 3 260 B ×2 | 2 |
| `/mvp` | 2 230 B ×2 | 3 |
| `/config/general` | 2 225 B ×2 | 4 |
| `/inventory` | 324 B ×2 | 1 |
| `/members` | 245 B ×2 | 1 |
| `/`, `/events`, `/audit-log`, `/health` | — | 0 (no `scripts` block; clean) |

The same nesting exists in `config/{boost,botprofile,creator,general,welcome}.html`, `general/member_profile.html`, `manage/{commands,customcommands,embedbuilder,reactionroles,tickets,triggers}.html`, `server_select.html`, `systems/{minigame_builder,tagmissions}.html`, `tagpartners.html` (same mechanism; not individually re-measured).

Document order on a full load of `/leveling` (byte offsets in the served HTML): `#content-area` 18 477 → **page script copy #1 41 368** → `dashboard.js` 64 932 → `nav-lifecycle.js` 65 495 → **page script copy #2 ≈78 949**.

**Step 2 — the second evaluation is fatal for scripts with top-level `const`/`let` (PROVEN runtime, `node vm`).**
Two classic scripts sharing one global realm: the second one declaring the same top-level `const` throws `SyntaxError: Identifier 'X' has already been declared`, and **nothing in that script runs** (a parse-time error; the binding is not created). `function`/`var` re-runs are harmless.

* `/leveling` declares `PRESTIGE_TIER_NAMES`; `/economy` `CUR_DEFAULTS`; `/minigames` 5, `/missions` 6, `/creator` 4, `/commands` 5, `/minigames/builder` 6+, `/reaction-roles` `buttons`/`colorMap`, `/custom-commands` `actions`, `/botprofile` `liveBotUsername`/`formDirty`, … — these pages therefore have a **hard** failure on any re-evaluation.
* Pages without a top-level `const`/`let` do not throw; instead **every function and listener is duplicated and every `loadX()` runs twice** (measured: `/trade`'s `/api/trade/history` fires ×2 on a single full page load; `/leveling`'s roles/channels pickers fetch 4×/1× in one init).

**Step 3 — htmx re-evaluates swapped scripts in the *existing* realm (PROVEN code + PROVEN runtime).**
htmx 1.9.10 (`allowScriptTags` default true) re-creates each `<script>` in the swapped fragment and inserts it during settle, *after* `htmx:afterSwap` — i.e. in the current page's realm, where copy #1's `const` is already bound.
Harness result, starting from `/trade` and navigating to `/leveling` with the real `htmx.ajax()` + `hx-target="#content-area" hx-swap="outerHTML" hx-select="#content-area"`:

| Event | API calls issued by the page script | `cfg-xp-min` | bonus-roles list |
|---|---|---|---|
| Full load of `/leveling` | 11 (config, reset, prestige, currency, boost, shop, bonus, blacklist, prestige-roles, roles ×4, channels) | `"5"` | rendered |
| **Nav 1** to `/leveling` (fresh realm: copy #1 declares the const and runs) | **11 — page initialises** | `"5"` | rendered |
| **Nav 2** to `/leveling` | **0** | `""` | `"Loading…"` |
| **Nav 3** to `/leveling` | **0** | `""` | `"Loading…"` |

with `SyntaxError: Identifier 'PRESTIGE_TIER_NAMES' has already been declared` on every nav from #2, and a successful 200/outerHTML swap each time (so the swap itself is fine — the panel is replaced with fresh *static* server HTML: empty inputs, `Loading…` placeholders).

**Consequences for the reported symptoms (PROVEN mechanism, INFERRED mapping to the exact anecdote):**

* A copy of the page DOM arrives (tables, cards, tabs) but every API-driven part stays empty — "partial render".
* **Whether a given click works depends only on whether that page's script has already been evaluated in the current JS realm.** The first evaluation after a *real* page load succeeds; every later one throws. So: panels stop initialising after the first in-page navigation to that page, and "needs a hard refresh" is exactly right.
* The literal "content appears on the second click" is **not** reproducible as such (clicks #2 and #3 were also dead in the harness). It is consistent with the same mechanism if the click that worked was the first visit *after a real load* (e.g. the page was reached from the address bar / a refresh in between), and it is also what an admin would report for the pages with **no** const conflict, where the first click *does* render — with everything double-initialised. Treat the exact click-count wording as INFERRED; the deterministic rule above is PROVEN.
* Nothing in the admin's console is surfaced in the UI: the only symptom is one `SyntaxError` line.

### 1.2 Secondary contributor, INFERRED: the in-content script runs before `dashboard.js`

The in-content copy (offset 41 368) precedes the shared scripts (`dashboard.js` 64 932, `nero-select.js` 64 980, `nav-lifecycle.js` 65 495). A fast async continuation of the page script (e.g. `loadPrestigeConfig()` → `renderPrestigeTiers()` using `currencyNameFor`, defined in `dashboard.js:127`) can therefore run **before** that helper exists. Reproduced once in the harness (`currencyNameFor is not defined`); in a real browser this is a timing race, so it can appear as an intermittent empty Prestige tab on a *full* load too. The fix direction (page JS registered after the shared bundle, or init deferred to a lifecycle hook) also removes this hazard.

### 1.3 Other lifecycle facts, PROVEN, that matter (and that are *not* the cause)

* Navigation is plain htmx: `hx-get` + `hx-target="#content-area"` + `hx-swap="outerHTML"` + `hx-select="#content-area"` (`base.html:125…`, and the sidebar lives outside `#content-area`, so nav links survive swaps). There is **no** `hx-boost`.
* `base.html:559-568`'s `htmx:afterSwap` handler re-creates every `<script>` inside the swapped target (a workaround for "swapped scripts don't run"). Measured effect in the harness: it does **not** cause a second execution per swap (per-nav API counts matched the per-copy expectation) — it is redundant with htmx's own handling, not an extra amplifier.
* Blank panel on **session expiry during a nav**: a 302 to `/login` has no `#content-area`, `hx-select` matches nothing, and the target is replaced with nothing (PROVEN code path; the same 302 was observed for `/config/announcements`). This is a second, independent "blank until refresh" path.
* A 4xx/5xx during nav is not swapped at all (htmx `shouldSwap` = 2xx–3xx and ≠ 204) and only emits `htmx:responseError` — so a failing nav looks like "the click did nothing", not like an error.
* **No polling / stale-cache problem on the dashboard**: only `minigames`/`minigame_builder` use `setInterval`; seven elements use `hx-trigger="load"` lazy loads. No client-side cache layer.
* **DB/backpressure is not implicated**: SQLite runs WAL with `busy_timeout`; XP/claim writes use `BEGIN IMMEDIATE`; no lock/timeout failures appeared in any probe. `dashboard/utils/async_utils.run_async` (persistent loop) is used correctly at 120+ call sites and is not the bug.
* The per-element `nsInit` guard in `nero-select.js` prevents some picker re-initialisation but does not stop duplicated `loadX()` calls.

### 1.4 Is Leveling special, or shared?

The mechanism is **shared** (23 templates), but the *visible severity* differs by page:

* Pages with top-level `const`/`let` (leveling, economy, minigames, minigame_builder, missions, commands, creator, botprofile, reaction-roles, custom-commands): hard dead on any htmx re-visit.
* Other duplicated pages: no crash, but double API calls, duplicated event listeners and duplicated `loadX()` (flicker, double toasts, doubled rows in the worst case, wasted requests).
* `/leveling` is also the page where an admin is most likely to *edit and save*, which is why the config defect (§2) shows up there first.

### 1.5 The smallest safe fix for the Dashboard issue (identified, NOT implemented)

Two mechanical changes per template, no logic rewritten:

1. **Stop the double emission:** inside `{% block content %}`, replace the nested `{% block scripts %}…{% endblock %}` with the plain `<script>…</script>` markup (drop the two block tags). The page script then stays **inside** `#content-area` (so htmx still runs it on navigation — un-nesting to `base.html`'s position is what makes htmx pages inert, which is exactly what `nav-lifecycle.js`'s header describes) but is emitted **once**.
2. **Make re-evaluation legal:** wrap the page script body in `(function(){ … })();` so its `const`/`let`/functions become function-scoped and every navigation may safely re-run the init (a remount).

Validated in the harness (pre-fix vs post-fix, same environment): with one emission + IIFE, **every** navigation re-initialises the page — `config` fetch +1, form values filled (`"5"`), bonus-roles list rendered, `SyntaxError` gone, on navs 1–4.

Caveats before doing it: (a) re-running the init means listeners/timers from the previous mount accumulate unless the page's own guards are idempotent — the repo's intended answer for that is the `nav-lifecycle` page-module registry (`data-page-module` + `data-page-script`, loaded once, `init/destroy` per mount), which is currently opt-in and **not** wired for any of these pages; (b) picker re-initialisation on navigation is currently not guaranteed (in the harness the `#cfg-levelup-channel` picker did not re-fetch roles after a nav) — verifiable in a browser before rollout; (c) do this pass for `/leveling` first, verify in a real browser, then roll it across the other 22 templates.

---

## 2. Exact cause of Save Config "Connection error"

**Chain, PROVEN (code + runtime).**

1. `saveConfig()` (`dashboard/templates/systems/leveling.html:515-540`) reads every field and posts it as a **string** (or `1`/`0` for checkboxes).
2. `ajaxSave()` (`dashboard/static/js/dashboard.js:222-258`) `POST`s JSON without checking `res.ok` first; its first body statement is `const data = await res.json();` (line 230).
3. `save_leveling_config_api()` (`dashboard/api/leveling.py:87-141`) converts with bare `int(data.get(...))` — 14 conversions, no `try/except`. `int("")` and `int("30.5")` raise `ValueError`.
4. Flask's `@app.errorhandler(500)` (`dashboard/app.py:275-277`) returns `errors/500.html` — **HTML with status 500**.
5. `res.json()` throws on the HTML body; the `catch` (line 252) is the **only** producer of the `Connection error` toast.

**Trigger classes, PROVEN by execution:**

| Input | Result | Row written? |
|---|---|---|
| any numeric field empty (e.g. after clearing it) | 500 HTML `int('')` | **no** (the tuple is built before the INSERT) |
| decimal value (`"30.5"`) | 500 HTML `int('30.5')` | **no** |
| other non-numeric string | 500 HTML | no |
| valid integers | `200 {"success": true}` | yes |
| failure **after** the commit (audit/log write, e.g. `log_action` raising) | 500 HTML | **yes — the change persists** |
| missing/incorrect CSRF header | `403 {"success": false, "error": "CSRF validation failed…"}` (JSON) | no — and the toast would be *that* message, **not** "Connection error" |
| expired session mid-save | 302 → `/login` HTML follows in fetch → `res.json()` throws → "Connection error" | no |

So: **"Connection error" is a lie about connectivity — it is a non-JSON (HTML) response.** "Saved despite the error" is real, but only for the post-commit class; the common blank/decimal case saves nothing.

**CSRF is not the cause (PROVEN).** `dashboard/app.py:106-118` enforces `X-CSRF-Token` on every non-GET `/api/*` (`403` JSON), and `dashboard/static/js/dashboard.js:16-33` patches **both** `window.fetch` (all non-GET) and htmx (`htmx:configRequest`) centrally. `leveling.html` itself contains no `X-CSRF-Token` string — it does not need one.

**Inconsistent response shapes are real (PROVEN):** the same blueprint answers `{success:true}` JSON, `{success:false,error}` JSON, HTML 500, HTML 403/404, and JSON 429. Only the config POST converts values without a guard; e.g. `/leveling/currency-reward` does `try: int(...) except (TypeError, ValueError): return {"success": false, "error": …}` (`dashboard/api/leveling.py:191-201`). The config POST is the odd one out, and the client's "parse JSON or call it a connection failure" contract turns that into a misleading message.

---

## 3. Leveling master toggle status

There are **three independent Leveling-related switches**, all defaulting to ON. "Master toggle" should mean the first one; the others are *not* the same control.

| # | Switch | Storage | Default when no row | Who writes | Who reads | Status |
|---|---|---|---|---|---|---|
| 1 | **XP engine master** | `leveling_config.enabled` | **1 (ON)** — `utils/xp_calculator.py:72-99` fallback | Leveling → Config tab (`cfg-enabled`) | message XP listener, voice-XP listener, `give_reward` for `reward_type == "xp"` (`utils/reward_engine.py:117-124`) | **effectively ON** |
| 2 | **Per-command enable** (7 commands: `rank`, `leaderboard`, `level`, `setxp`, `resetxp`, `resetleaderboard`, `prestige`) | `command_toggles.enabled` | **no row ⇒ allowed** (`utils/command_gating.py:85-93`) | Commands page (per-command switches + Enable All / Disable All per category) — **not** on the Leveling page | `NeroCommandTree.interaction_check` → `check_command_toggles` (slash **and** alias paths) | **effectively ON** |
| 3 | Prestige earn multipliers `prestige_config.enabled` | `prestige_config` | see `utils/prestige.py` | Leveling → Prestige tab | prestige earn multiplier lookups | separate system (left out of scope per your constraint) |

**The dashboard does not show the true #1 state on a fresh guild — PROVEN runtime.** `GET /api/leveling/config` returns `{"config": {}}` when the guild has no row (`dashboard/api/leveling.py:69-84`). `loadConfig()` then calls `set(id, undefined)` for every field: checkboxes become `!!undefined` → **unchecked**, numeric fields `undefined ?? ''` → **blank**, the channel picker → empty. Reproduced in the harness with `{"config":{}}`:

```
enabled=OFF  xp_per_word=""  xp_min=""  xp_max=""  cooldown=""
voice_enabled=OFF  voice_xp=""  voice_unmuted=OFF  spam=OFF  spam_thresh=""
announce=OFF  remove_old=OFF
```

while the bot runs with `enabled=1` and the full default set. Consequences: (a) the toggle *looks* off while the bot earns XP; (b) an admin who clicks "Save Config" in that state posts `""` for every numeric field → the 500/"Connection error" from §2; (c) this directly violates your acceptance criterion that existing/default values display automatically.

**Live value caveat (NEEDS A DECISION/INFO):** the repo has no DB (`DATABASE_PATH` defaults to `/app/data/nero.db`), so the *deployed* `leveling_config.enabled` value cannot be read from the checkout. Reading it requires the deployment DB. Everything above is about effective defaults and dashboard rendering, not the production row.

**Also worth knowing (PROVEN):** `enabled` gates **only XP rewards**. `give_reward` for coins/diamonds/roles is unaffected (`utils/reward_engine.py:54-116`), so turning Leveling off does not break Shop/Mission/economy payouts. One exception exists — `/api/edit-member` (`dashboard/app.py:2651-2700`) writes `levels.xp/level` **without consulting `enabled`** (it is an explicit admin edit; flag for a product decision in §9/§12).

---

## 4. Current XP / default configuration

### 4.1 Effective defaults (no DB row) — PROVEN code

Source: `utils/xp_calculator.py:72-99` (fallback dict) plus the hard-coded runtime fallbacks used by the listeners (`config.get(key, default)` in `cogs/leveling.py`).

| Setting | Fallback value | Dashboard field? | API write? | Runtime consumer |
|---|---|---|---|---|
| `enabled` | 1 | ✅ `cfg-enabled` | ✅ | XP gate (3 paths) |
| `xp_per_word` | 1 | ✅ | ✅ | message XP |
| `xp_min_per_message` | 5 | ✅ | ✅ | message XP |
| `xp_max_per_message` | 50 | ✅ | ✅ | message XP |
| `xp_cooldown_seconds` | 30 | ✅ | ✅ | message XP cadence |
| `voice_xp_enabled` | 1 | ✅ | ✅ | voice listener |
| `voice_xp_per_minute` | 3 | ✅ | ✅ | voice listener (per 60 s tick) |
| `voice_require_unmuted` | 1 | ✅ | ✅ | voice listener (mute **or** self-mute) |
| `spam_detection_enabled` | 1 | ✅ | ✅ | message listener (pre-cooldown) |
| `spam_threshold` | 3 | ✅ | ✅ | anti-spam |
| `spam_xp_penalty` | 10 | ✅ | ✅ | anti-spam |
| `spam_window_seconds` | **10 (hard-coded `.get(..., 10)`)** | ❌ | ❌ (not in the POST) | anti-spam window |
| `levelup_announce` | 1 | ✅ | ✅ | level-up announce |
| `levelup_channel_id` | NULL (= same channel) | ✅ picker | ✅ (`""` → NULL) | level-up announce |
| `levelup_message` | NULL (embed form) | ✅ | ✅ (`""` → NULL) | level-up announce |
| `levelup_embed_data` | NULL | ❌ | ❌ | **no reader anywhere — dead column** |
| `remove_old_reward_role` | 0 | ✅ | ✅ | **only read by dead code** (`utils/xp_calculator.py:282`, inside `check_and_award_level_rewards`, 0 callers) |

### 4.2 The maths, as implemented — PROVEN code

* **Message XP** = `clamp(word_count × xp_per_word, xp_min_per_message, xp_max_per_message) × role_multiplier × boost_multiplier`; `0` when `enabled` is falsy or a blacklist role matches. Bonus-role multipliers do not stack (highest wins).
* **Voice XP** = `int(minutes × voice_xp_per_minute)` with `minutes` hard-coded to `1` per 60 s tick (`cogs/leveling.py:396`); no role/boost multipliers; no level-up announcement.
* **Level curve** = `floor(100 × level^1.5)` per level, iterated by `xp_progress`. Node costs: L10 3 162 · L25 12 500 · L50 35 355 · L75 64 951 · L100 100 000; cumulative: L50 351 655 · L75 1 981 105 · **L100 4 050 079**.
* Every XP grant is logged to `transaction_ledger` (`currency='xp'`, reason `Message XP` / `Voice XP`, source) by `give_reward` → `_log_xp_ledger`; the **spam penalty bypasses that** (raw `UPDATE levels`, `cogs/leveling.py:301`).

---

## 5. Level-100 simulation for the 6 h/day × ~10-month target

Method: Python simulation that re-implements the live gate order exactly (spam check → 30 s cooldown → XP math → voice tick at 60 s) with a simulated clock; the live listeners were not executed. Script: `/tmp/neuro/xp_sim2.py`.

**Ceilings and outcomes at 6 h/day (defaults, 8-word messages):**

| Message rate | Voice XP | XP/day | 300-day XP | 300-day level |
|---|---|---|---|---|
| 30/h | off / on | 1 440 / 2 520 | 432 k / 756 k | 40 / 50 |
| 60/h | on | 3 960 | 1.19 M | 61 |
| 90/h | on | 5 400 | 1.62 M | 69 |
| **120/h (= 30 s cooldown ceiling)** | off | 5 760 | 1.73 M | 70 |
| **120/h (ceiling)** | on | **6 840** | **2.05 M** | **76** |
| 240/h (above ceiling → no more XP) | on | 6 840 | 2.05 M | 76 |

**Verdict: Level 100 is not reachable in ~10 months with current defaults.**

* L100 needs 4 050 079 XP = **13 500 XP/day**; the maximum sustained rate is 5 760 (chat) + 1 080 (voice) = **6 840/day** ⇒ **≈592 days with voice, ≈703 days without** (≈19.5–23 months).
* The binding constraint is the **30-second cooldown** (120 XP-messages/hour), not the curve: with the cooldown removed the same 6 h/day reaches L76–L97. Voice XP is a small contributor (≤1 080/day ≈ 16 %).
* Words are the biggest lever: at the ceiling, 12 words/msg → L87, 20 words/msg → **L105** in 300 days (the `xp_max_per_message = 50` cap allows up to 50 words' worth).
* **Anti-spam makes it worse for fast chat:** with 20 % of messages in 3-message bursts 3 s apart (defaults: 3 per 10 s ⇒ −10 XP and no XP for that message), 120 msgs/h drops from L61 → L55, 240/h from L70 → L42.

NEEDS A DECISION: whether the 10-month target is meant to be reachable (then the levers are `xp_cooldown_seconds`, `xp_per_word`/words or `xp_max_per_message`, and/or the anti-spam penalty), or whether L100 is intentionally a long-haul goal (~20 months).

---

## 6. Voice XP — current behaviour and feasibility of a second independent toggle

### 6.1 Current behaviour — PROVEN (code), exact order

`cogs/activity_engine.py:82-125` (`voice_tick_task`, 60 s loop) is the sensor:

1. skip the guild's AFK channel;
2. **hard requirement: `real_members = [m for m in channel.members if not m.bot]`; `if len(real_members) < 2: continue`** — a member alone in a channel is never ticked, regardless of any setting;
3. skip `self_deaf`/`deaf` members;
4. increment `activity_stats.voice_minutes` for the day;
5. dispatch `activity_voice_tick(guild, member, flags)` with `self_mute`, `mute`, `self_deaf`, `deaf`, `channel_id`.

`cogs/leveling.py:372-408` is the policy layer (per tick): `enabled` → `voice_xp_enabled` → `voice_require_unmuted` (skip if muted/self-muted) → blacklist roles → `calculate_voice_xp(1, per_minute)` → `give_reward(...)`.

So today `voice_xp_enabled=1` *implies* "must not be alone", and the dashboard states it as a fixed rule: *"Voice XP is never given when alone, deafened, or in AFK channel."* (`leveling.html:113`). There is **no** configuration for it.

### 6.2 Feasibility of "require another participant" as an independent toggle

**Not implementable in the listener alone (PROVEN):** when the member is alone the tick is never dispatched, so the Leveling listener has no event to opt into. The gate is upstream, in the engine.

**Cross-system coupling (PROVEN):** the same tick drives three consumers — `cogs/leveling.py:372`, `cogs/missions.py:528` (`record_activity(..., "voice_minutes", 1)`), `cogs/mvp.py:81` (`mvp_scores`, `voice_minute_weight`, default 2.0) — plus the engine's own `activity_stats.voice_minutes` write. Any change to the gate must say explicitly what those three do about solo minutes.

**Design options (NEEDS A DECISION):**

* **A — sensor/policy split (recommended):** the engine computes a new flag `others_present` (≥1 other non-bot member in the channel) and no longer hard-skips solo members; it **keeps** `activity_stats.voice_minutes` and keeps the tick stream for missions/MVP unchanged (either by only dispatching the "solo" ticks through a distinct event for Leveling, or by having all three consumers check `others_present` — the latter is cleaner but touches missions/MVP). The Leveling listener then applies the new **`leveling_config.voice_xp_require_other`** (`1` default = current behaviour, `0` = earn while alone).
* **B — policy in the engine:** the engine reads `leveling_config` for the guild and relaxes the ≥2 rule when the toggle is off. Smallest code diff, but it makes a shared sensor leveling-aware and silently changes missions/MVP/`voice_minutes` semantics.

**Semantics to preserve (your constraint):** OFF = can earn while alone; ON = require ≥1 other **real, non-bot** participant in the **same channel**; independent from `voice_xp_enabled`; dashboard-controllable (new column + Config field + POST field + GET surfacing).

Open questions for A/B: should a *deaf/self-deaf* other member count as "present" (today the ≥2 rule counts them)? Should solo minutes count toward `activity_stats.voice_minutes`/missions/MVP when the toggle is OFF (if yes, the 6 840/day ceiling in §5 gains up to 1 080 XP/day while alone)? The UI hint text must be rewritten either way.

---

## 7. Anti-spam — exact semantics in plain language

Code: `cogs/leveling.py:246-254` (`_is_spamming`) and `:283-300` (gate). Defaults: detection ON, **threshold 3 messages**, **window 10 s** (hard-coded default; not editable in the dashboard), penalty **10 XP**.

* The check runs on **every** message, *before* the XP cooldown gate (a deliberate fix documented in the code: previously the cooldown filtered out everything the tracker needed, so spam could never fire).
* The tracker keeps, per `(server, member)`, the timestamps of recent messages. It drops timestamps older than the window, appends "now", and reports spam when `len(times) >= threshold` — so with defaults the **third message inside 10 s** is the first flagged one.
* While the window is still full, **every further message is also flagged** (the condition stays true), each costing `spam_xp_penalty` XP (subtracted from total XP, floored at 0) and earning **no** XP itself. A flagged message also **does not consume the XP cooldown** (the handler returns early) — it is purely a deduction.
* The tracker is **in bot memory** and keyed per member per server: a bot restart clears it; it is not per-channel; the tracker is never pruned (unlike the cooldown dict, which has a pruning pass) — a minor, bounded memory-growth wart.
* It never deletes or blocks messages; moderation/timeouts stay with the moderation system.

Interpretation for the admin UI: *"If a member sends 3 messages within 10 seconds, that message and every further message until the window drains loses 10 XP and earns none."*

---

## 8. `/resetleaderboard` — exact table-by-table behaviour

Same function powers the slash command (`cogs/leveling.py:646-654`), the dashboard's Force Reset (`dashboard/api/leveling.py:489-506`) and the 30-minute scheduled task (`cogs/leveling.py:478-521`). Whole body runs in **one `BEGIN IMMEDIATE` transaction** (all-or-nothing) and is scoped to one guild.

| Table | Effect | Note |
|---|---|---|
| `levels` | `UPDATE levels SET xp = 0, level = 0 WHERE guild_id = ?` | rows are kept; `prestige` is **not** touched (legacy prestige survives a reset) |
| `leveling_leaderboard_history` | **INSERT one row per member** of the guild: `(guild_id, user_id, xp, level, rank, period, period_end=now)` | the ranking is `xp DESC` at reset time; `rank` is the position in that ordering; **prestige is not archived**; no de-duplication — repeating a reset appends a new snapshot |
| `leveling_reset_config` | `INSERT … VALUES (guild, 1, period, now) ON CONFLICT DO UPDATE SET period, last_reset` | **for a guild with no row this creates one with `enabled = 1`**, i.e. a one-off manual reset silently *arms* the recurring auto-reset; `enabled` is not modified on conflict, so an explicit dashboard "off" is preserved |
| `levelup_channel_id` target | the scheduled task posts a "Leaderboard Reset" embed; the slash command replies ephemerally to the admin | the command itself posts nothing in-channel |

**Not touched (PROVEN):** `level_reward_claims` (entitlements, statuses, `fulfilled_at`), `leveling_rewards`, `leveling_currency_rewards`, `leveling_shop_rewards`, `leveling_boost_rewards`, `leveling_bonus_roles`, `leveling_blacklist_roles`, `leveling_active_boosts`, `activity_stats`, `economy`, `prestige_config`/`prestige_roles`.

**Consequences to be explicit about:**

1. Every member drops to Level 0 and the `/level` card shows `Level 0`, while **previously unclaimed crossings remain claimable** (claims come from the tracked `level_reward_claims` snapshots, not from the current XP) — and "Ready to claim" counts them.
2. The leaderboard ordering becomes prestige-first, then XP, so a prestiged member stays above everyone with XP after a reset.
3. **The armed recurrence:** anyone who runs `/resetleaderboard` (or the dashboard Force Reset) on a guild that never configured resets gets `enabled=1` + `last_reset=now` → the 30-minute task will wipe and archive the whole guild leaderboard again **every 7 days** (or 30, if `period` was monthly) until an admin disables it on the Leveling → Leaderboard Resets card. This is the single most user-visible side effect of the command.
4. `/resetxp` (single member) is unrelated and non-destructive to history: it only sets that member's `levels` row to `xp=0, level=0`; nothing is archived.
5. `prestige` re-purchase state, boosts, claims and definitions all survive, so a "reset" is a rollover, not a wipe of reward progress.

---

## 9. Dashboard ↔ Bot Leveling synchronization audit (per setting)

Legend: **✔ sync** (dashboard write → same value read by the bot), **⚠ display** (dashboard displays something other than the effective value), **⚠ no-op** (dashboard-writable but nothing live reads it), **⚠ missing** (live behaviour with no dashboard surface), **⚠ gate-bypass** (a writer that ignores a configured gate).

| Setting / behaviour | Storage | Dashboard read | Dashboard write | Bot consumer (live) | Verdict |
|---|---|---|---|---|---|
| `enabled` (XP master) | `leveling_config.enabled` | ✅ but shows OFF on no-row guilds | ✅ | 3 XP paths + `give_reward` | ⚠ display |
| `xp_per_word`, `xp_min/max_per_message`, `xp_cooldown_seconds` | `leveling_config` | ✅ | ✅ | `calculate_message_xp` | ✔ |
| `voice_xp_enabled`, `voice_xp_per_minute`, `voice_require_unmuted` | `leveling_config` | ✅ | ✅ | voice listener | ✔ |
| **"require another participant"** | — | — | — | hard-coded in `activity_engine` | ⚠ missing (§6) |
| `spam_detection_enabled`, `spam_threshold`, `spam_xp_penalty` | `leveling_config` | ✅ | ✅ | message listener | ✔ |
| `spam_window_seconds` | column exists (ALTER, default 10) | returned by `SELECT *` but no field | ❌ absent from the POST | message listener | ⚠ missing (not editable) |
| `levelup_announce`, `levelup_channel_id`, `levelup_message` | `leveling_config` | ✅ | ✅ | `_announce_levelup` | ✔ |
| `levelup_embed_data` | column exists | — | — | **nothing** | ⚠ dead (no-op) |
| `remove_old_reward_role` | `leveling_config` | ✅ | ✅ | **only dead code** reads it; the live claim path only *adds* roles (`utils/level_claims.py:378-391`) | ⚠ no-op |
| Level rewards (`leveling_rewards`) | table | ✅ | ✅ CRUD | claim definitions / delivery | ✔ |
| Currency / shop / boost rewards | 3 tables | ✅ | ✅ CRUD | claim definitions / delivery | ✔ |
| Bonus roles (`leveling_bonus_roles`) | table | ✅ | ✅ CRUD | message XP multiplier | ✔ |
| Blacklist roles (`leveling_blacklist_roles`) | table | ✅ | ✅ CRUD | message **and** voice XP | ✔ |
| Active XP boosts (`leveling_active_boosts`) | table | ❌ no surface | ❌ (shop/engine only) | message XP multiplier with `expires_at` | ⚠ missing (read-only gap) |
| Reset config (`enabled`, `period`, `last_reset`) | `leveling_reset_config` | ✅ | ✅ (enabled/period) | scheduled reset + force reset | ⚠ side-effect: force reset creates `enabled=1` (§8) |
| Prestige config / tier role mapping | `prestige_config`, `prestige_roles`, `prestige_tiers` | ✅ (Leveling → Prestige tab) | ✅ | prestige multipliers, role grants | ✔ (out of scope, listed for completeness) |
| Per-command enable / roles / channels / cooldown | `command_toggles` | ✅ but on the **Commands** page, not Leveling | ✅ | `NeroCommandTree.interaction_check` + alias path | ✔ wiring, ⚠ discoverability |
| Admin XP edit (`/api/edit-member`) | `levels` | members page | ✅ | writes XP/level + `record_crossing(SOURCE_DASHBOARD)` | ⚠ gate-bypass (ignores `enabled`; explicit admin action, needs a decision) |
| Claim state (`level_reward_claims`) | table | ❌ no dashboard surface | ❌ | claim/deliver engine | ⚠ missing (no admin visibility of pending/failed claims) |

**Is the architecture ready for a later all-category audit? Yes (assessment).** The dashboard follows one uniform pattern (page template + `{% block scripts %}` + `fetch`/htmx → `@require_api_permission`); the settings each live in one table; the bot reads them via `config.get(...)` or a table query. A cross-category audit is therefore **automatable**: for every page template, extract the `id="cfg-*"`/named inputs, diff them against the API's INSERT/UPDATE columns, and diff those columns against the keys the bot's `config.get(...)` calls read in the same module — the three lists should be identical, and every discrepancy is exactly the ⚠ rows above. **Start with that generator (one script, no hand-auditing), and start with Leveling** because it is the most complex (3 gates, 5 reward tables, reset config, two write paths) — if the generator produces no false positives on Leveling it will be safe on the simpler pages.

---

## 10. `/level` — current architecture and lifecycle

**Command path (PROVEN code + runtime):** `/level` (`cogs/leveling.py:656-666`) → `defer(ephemeral=True)` → `_level_embed(guild, user, "level")` → `LevelRewardView(bot, guild_id, user_id)` attached to the **ephemeral** follow-up.

* **The embed** (`_level_embed`, `:108-171`) computes the live XP/level (`xp_progress`), lists the member's claims, and renders one line per reward configured for the current level, preferring the **frozen claim payload** over the current definition (deleted shop products/boost rows still render from the claim). Statuses shown: `pending` / `processing` / `fulfilled` / `failed` (+ "— failed, can retry"), or `not crossed`. A footer notes how many *earlier* rewards "Claim All" would also collect; `page == "stats"` renders the five-field Stats embed (§11).
* **The view** (`LevelRewardView`, `:183-220`): `timeout=180`, three decorator buttons (**no `custom_id`**), no `interaction_check`. Reproduced against installed discord.py 2.7.1: two instances get **different random** custom_ids, and `is_persistent()` is **False**. It is never registered with `add_view`, so it lives only in discord.py's per-message view store. **Lifecycle consequence (PROVEN):** after 180 s, or after any bot restart, the view is gone; discord.py's dispatch then fails to resolve the button (`item.view is None` → a warning log, silent to the user) and Discord shows "This interaction failed". The buttons on a `/level` card are therefore a **3-minute** affordance, and the Command gating (`enabled`, roles, channels) applies at *invocation*, not to the button press.
* **"Claim All"** (`:198-219`): ownership check → `defer()` (role delivery can exceed the ack window) → `claim_available()` reserves rows with a fresh `owner_token` under `BEGIN IMMEDIATE`, delivers payouts in SAVEPOINTs (roles are added **after** the commit via `deliver_role`), records `last_error` on failure and leaves those rows retryable → re-renders the embed with a "Claimed N. Retryable failures: M." footer.
* **State model** (`utils/level_claims.py`): one row per `(guild, user, reward_level, track, reward_ref)` in `level_reward_claims`, `status ∈ {pending, processing, fulfilled, failed}`, `owner_token`/`processing_started_at` as a lease, `created_at`/`fulfilled_at`, `last_error`, `source` (e.g. `leveling` for XP grants, `setxp`, `dashboard`, `legacy_backfill`); index `(guild_id, user_id, status)`. `/rank` remains the profile/rank card and is untouched by this design.

---

## 11. What a richer Stats view can safely show with the existing data model

Today `/level` → **Stats** shows Level / Total XP / Progress / Ready to claim / Fulfilled — the five you have ruled out as the *whole* of Stats. Below, "available" means the current schema already answers it without new writes; the §9 Stats reward-status list is treated as a product direction to evaluate, item by item.

| Candidate stat | Source (existing) | Verdict |
|---|---|---|
| Claims by status (`pending` / `processing` / `fulfilled` / `failed`) | `level_reward_claims.status` (indexed) | **safe now** |
| Claims by track (role / currency / shop / boost) and by level | `track`, `reward_level` | **safe now** |
| **Failed claims with the stored error**, retryable or not | `status='failed'`, `last_error` | **safe now** (free-text errors; group them) |
| Oldest unclaimed / claim age | `created_at`, `fulfilled_at` | **safe now** (also gives delivery latency distribution) |
| Claim provenance (how the crossing was recorded) | `source` (`setxp`, `dashboard`, `legacy_backfill`, level-up) | **safe now** |
| Rewards configured but *not* yet crossed, per level | definitions derived from the 4 reward tables + `progress_level` | **safe now** |
| Levels with **no** reward configured | same | **safe now** (useful admin gap list) |
| Rewards for levels **below** current level that were never claimed | `reward_level` vs current level | **safe now** |
| Current XP / level / rank / percentile / total ranked / prestige / member-since / messages / voice minutes / minigame wins / balance / diamonds / equipped role + title / inventory grid | `utils/rank_card_data.get_rank_card_data` (:150-170) | **safe now** (already aggregated) |
| Daily activity (messages, words, voice minutes, forum posts) | `activity_stats` — **days are Cairo-local** (`get_cairo_daily_key`) | **safe now with a caveat**: label the day boundary; voice minutes only accrue under the ≥2 gate (§6) |
| Historical cycle standings (past resets) | `leveling_leaderboard_history` (`xp`, `level`, `rank`, `period`, `period_end`; indexed by `(guild_id, period_end)`) | **safe now**, but note: no prestige and no reward columns are archived |
| XP gained per period / chat-vs-voice split | `transaction_ledger` (`currency='xp'`, `amount`, `reason`, `source`, `created_at`, indexed) | **safe with a caveat**: spam penalties are *not* logged (raw `UPDATE`), so ledger sums overstate pre-penalty XP, and `reason` strings are the only label |
| Active boosts (multiplier, expiry) | `leveling_active_boosts` | **safe now**; no *historical* consumed/expired boost record |
| "XP lost to spam" | — (never persisted) | **not available** without a new write |
| Time-to-claim per member / per level, delivery success rate for non-claim paths | only derivable per claim row | **partially available** |
| Anything time-series about XP **before** the ledger existed | — | **not available** |

Presentation guidance that follows from the data: the safe, non-duplicating backbone is **claim-state breakdown + failed/retryable claims + unclaimed older levels + config coverage gaps + historical cycle snapshots**, optionally decorated with ledger-derived XP gains (labelled as pre-penalty) and daily activity (labelled Cairo days). Nothing here requires touching the claim engine or the reward tables.

---

## 12. Missing / wrong / wired-only settings

**Missing (live behaviour with no dashboard control)**

1. `voice_xp_require_other` — the §6 toggle (also needs the engine change; the UI text currently states the rule as fixed).
2. `spam_window_seconds` — live in the anti-spam window (`config.get("spam_window_seconds", 10)`), column exists with default 10 via ALTER (`database.py:689-694`), but **no dashboard field and it is absent from the POST** (`dashboard/api/leveling.py:98-133`) → not editable short of a DB edit.
3. No read-only surface for `leveling_active_boosts` (an admin cannot see who has an active XP boost).
4. No dashboard visibility for claim state (`level_reward_claims`) — failures and retryable claims exist only in the `/level` card and logs.
5. Per-command Leveling toggles live on the **Commands** page; nothing on the Leveling page links there, so the category's "master toggle #2" is easy to miss.

**Wired-only / no-op**

6. `remove_old_reward_role` — the only reader is `check_and_award_level_rewards` (`utils/xp_calculator.py:282`), which has **zero callers**; the live claim path adds roles and never removes old ones (`utils/level_claims.py:378-391`). The dashboard checkbox has no runtime effect. Decide: implement (remove superseded level roles at delivery) or remove the setting.
7. `levelup_embed_data` — column + fallback key only; no reader, no writer, no UI. Dead column (and `levelup_message` is the live mechanism).

**Wrong / hazardous**

8. `POST /api/leveling/config` has no input validation (bare `int()`) → HTML 500 → "Connection error"; the other leveling POSTs already use the correct pattern. (§2)
9. `GET /api/leveling/config` returns `{"config":{}}` on a no-row guild and the page renders that as "everything off, everything blank", contradicting the bot's effective defaults (§3/§4).
10. `perform_leaderboard_reset` creates `leveling_reset_config` with `enabled = 1` for guilds that never opted in, arming the recurring auto-reset (§8).
11. `/api/edit-member` writes XP/level without the `enabled` gate (§3) — deliberate admin action, but it should be a conscious product decision.
12. Dashboard page scripts are emitted twice and re-evaluated in one realm (§1); the in-content script also precedes `dashboard.js` in document order (§1.2).
13. `_spam_tracker` (per-member timestamp dict) is never pruned, unlike the cooldown dict that received a dedicated pruning fix (`cogs/leveling.py:246-254` vs `utils/command_gating.py:42-56`). Minor.

---

## 13. Smallest safe implementation plan (logical slices)

Ordering principle: fix the **environment** before touching any Leveling UI (otherwise every manual test is unreliable), then fix the error surface, then the display/contract, then add the one genuinely new feature (Voice XP toggle). Nothing here requires changing the XP formula, the claim/entitlement engine, or the Feed/Shop/Prestige systems.

**S1 — Page-script lifecycle (fixes the dashboard-wide loading issue) · lowest risk, highest payoff**
Scope: `dashboard/templates/**/*.html` (23 templates, start with `systems/leveling.html`) + optionally `base.html`'s redundant afterSwap script re-creation.
Change: (a) inside `{% block content %}`, drop the `{% block scripts %}…{% endblock %}` tags, keeping the plain `<script>` where it already is; (b) wrap the script body in an IIFE so re-evaluation on every htmx navigation is legal.
Verification: served HTML contains the block once; a browser test does Dashboard → Leveling → Economy → Leveling with the panel initialising each time and no `SyntaxError`; no duplicate API calls on a full load.
Note: harness-validated (see §1.5). Do **not** simply move the block outside `content` — that makes htmx pages inert (this is what `nav-lifecycle.js` was written to fix). A later, larger slice can migrate page JS into `nav-lifecycle` `data-page-module` files to get real `destroy()`/teardown and script ordering after `dashboard.js`.

**S2 — Save-path error contract (fixes "Connection error" / "saved anyway") · tiny diff**
Scope: `dashboard/api/leveling.py` (config POST) + optionally `dashboard/app.py`'s `log_action` call.
Change: validate/coerce the 14 integers in a `try/except (TypeError, ValueError)` returning `{"success": false, "error": "…"}` (mirroring `/leveling/currency-reward`); make the post-commit audit step non-fatal so a 200 is returned when the row was written.
Verification: POST with `""`, `"30.5"`, all-valid; expect a readable JSON error for the first two, `{"success": true}` and a persisted row for the third; no HTML 500 on the config route.

**S3 — Effective-config display (fixes the fresh-guild OFF/blank mismatch) · small**
Scope: `GET /api/leveling/config` (merge the missing keys from the same fallback the bot uses) and/or `loadConfig()` (apply the fallback client-side), plus surfacing `spam_window_seconds`.
Change: the API (or the page) must always return the *effective* 16 keys; the master toggle then displays truthfully and an untouched Save writes back the same effective values instead of failing.
Verification: fresh guild page shows `enabled=ON`, 1/5/50/30, voice 3, spam 3/10, announce ON; Save without edits → 200 and a row equal to the defaults; subsequent loads show the persisted values.

**S4 — Voice XP "require another participant" toggle · the only new feature; needs the §6 decision**
Scope: `database.py` (new `leveling_config.voice_xp_require_other` column, default 1), `cogs/activity_engine.py` (pass `others_present`, stop hard-skipping solo members per design A/B), `cogs/leveling.py` (read the toggle), `dashboard/api/leveling.py` (POST/GET), `dashboard/templates/systems/leveling.html` (field + hint text), and — only for design A with `others_present` checks — `cogs/missions.py`/`cogs/mvp.py`.
Verification: OFF ⇒ a member alone earns `voice_xp_per_minute` per minute; ON ⇒ exactly today's behaviour; missions/MVP/`activity_stats` numbers unchanged for the ON case (test both).
Decision needed first: whether solo minutes count for stats/missions/MVP, and whether a deaf/self-deaf other member counts as "present".

**S5 — Cleanups, each independently shippable**
`remove_old_reward_role` implement-or-delete; `levelup_embed_data` drop-or-use; claim-state visibility for admins (read-only Stats block per §11); anti-spam tracker pruning; surface the Leveling command toggles (or link to the Commands page) from the Leveling page.

**Not in scope / deliberately untouched:** the XP curve and formula; the claim/entitlement ownership and concurrency design; Prestige; Shop; the Stats reward-status list is treated as product direction (§11), not as an implementation instruction; the wider all-category Dashboard↔Bot audit (feasibility and starting point are in §9).

---

### Appendix — evidence artefacts (outside the repo)

* Flask probes: `/tmp/neuro/probe{2,3,4,5,6,7,8,9}.py` (routes, server-rendered duplication, CSRF, 500 classes, DB persistence after 500).
* DOM harness: `/tmp/neuro/harness2.js` (real page + real htmx, pre-fix behaviour), `harness4.js` (fresh-realm first nav works, later navs dead), `harness5.js` (candidate fix validated), `harness6.js` (fresh-guild `{}` config rendering).
* XP rate simulation: `/tmp/neuro/xp_sim2.py`.
* Realm semantics: `node vm` two-classic-script test (top-level `const` re-declaration throws; `function`/`var` do not).
* Real page snapshot used by the harness: `/tmp/neuro/leveling_page.html` (89 964 B, captured from the live app with a forged session).
