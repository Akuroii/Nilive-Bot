# Minigames v2 — Final Implementation Plan (v2, all decisions locked)

> **Status:** FINAL v2 — presented for the last review before the first code change.
> **No code has been written or modified yet.**
>
> Round-3 sign-offs incorporated: empty reward pool = valid configuration (D11);
> category disable = automatic-rotation branch exclusion only, nothing deleted or
> modified (D10 refined); exact user-facing toggle copy; additional worked
> examples for the recursive selection; explicit concurrency section.
>
> **Standing working rules (agreed round 3):**
> * Defaults and recommended settings are **never hard restrictions** unless
>   technically necessary.
> * Improvements within the agreed scope (UX, validation, maintainability) are at
>   my discretion while implementing.
> * Any change touching **architecture, security, data integrity, user-facing
>   behavior, or the agreed scope** is flagged and reviewed **before** being made.
> * No unrelated features are introduced.

---

## A. Decision log (complete — nothing left open)

| # | Decision |
|---|---|
| D1 | Category tree is genuinely recursive for spawn: traversal descends until a node whose direct templates can produce a game. A node with both direct templates and subcategories offers **both** as valid paths. |
| D2 | Quick Click: buttons disabled before reveal; one **random-position** button turns green after a configurable min/max reveal delay; post-reveal wait configurable. |
| D3 | RPS: private ephemeral choice controls per seated player; seating timeout and choice timeout both dashboard-configurable; any timeout before a valid state → **no winner, no reward**. No forfeit-by-default anywhere. |
| D4 | Test Spawn: via bot request queue, `[Test]` marked, never touches weekly counter/probability, logged as `mode='test'`, uses the exact same real engine/buttons/timers/rewards. ~10s queue delay accepted. |
| D5 | Weekly counter increments **only** on a successful automatic spawn. Manual and test never count. The weekly min/max system decides only *whether* an automatic spawn happens. |
| D6 | Answer button text = exactly what the admin typed. No auto A/B/C/D prefix, no modification. |
| D7 | Quick Click buttons auto-numbered `1 2 3 4 5` per configured count. |
| D8 | No template weights in v1 — uniform probability within a selection level (shuffle bag). |
| D9 | Template toggles: `enabled` (master) + `auto_spawn` (rotation). Confirmed semantics + UI copy in §13. |
| D10 | Category `enabled` = **automatic-rotation eligibility for the whole branch** (including nested subcategories/templates). Disabling deletes nothing, modifies nothing, permanently disables nothing; re-enabling restores eligibility. It does **not** block manual/test spawning of individual templates (their own toggles govern those). |
| D11 | **Empty reward pool is a valid configuration.** The game runs normally, winners are still determined and shown, no rewards are granted, and the log records that the game had no rewards. Never treated as a configuration error. |
| D12 | Spawn permission matrix — formalized (also in §8): automatic = `enabled ∧ auto_spawn ∧ enabled ancestor chain`; manual (specific template) = `enabled` (category state irrelevant); manual (no template) = same recursive eligibility as automatic; test = always allowed (admin-only), never counted. |

---

## 1. Final architecture

Two processes, one source of truth — the pattern the project already runs:

```
┌────────────────────────┐         ┌─────────────────────────────┐
│ DASHBOARD (Flask)      │  SQLite │ BOT (discord.py)            │
│  - admin UI (Jinja +   │◄───────►│  - daily_check_loop (weekly │
│    vanilla JS)         │  WAL +  │    trigger, kept verbatim)  │
│  - API blueprints      │  busy_  │  - spawn_request_loop (10s) │
│  - writes config/CRUD/ │  timeout│  - SELECT + SPAWN (owns all │
│    spawn requests      │         │    live games: views,       │
│  - never holds the     │         │    timers, embed edits,     │
│    Discord token for   │         │    reward grants via        │
│    minigames           │         │    utils/reward_engine)     │
└────────────────────────┘         └─────────────────────────────┘
```

* The dashboard **never** runs a live game engine (interactive components need
  the bot's websocket + `discord.Client`). Test/Manual spawn = a row in
  `minigame_spawn_requests`; the bot polls every 10s and executes it with the
  real engine (D4). The existing REST-direct dashboard pattern is **not**
  extended.
* All minigame SQL lives in `utils/minigame_store.py`; all engine logic in
  `utils/minigame_engine.py` (project rule: engines in `utils/`, like
  `reward_engine.py`); the cog (`cogs/minigames.py`) owns the Discord surface.
* Reward delivery: exclusively `utils.reward_engine.give_reward()` — the single
  shared engine every reward path in this project already uses.
* Weekly pacing: `daily_check_loop` + `compute_daily_probability` kept verbatim.

---

## 2. Database / schema changes (complete)

New tables via the minigames module's idempotent `ensure_tables()`
(`CREATE TABLE IF NOT EXISTS` + guarded `ALTER`s) — the exact pattern the
current minigames tables use (dashboard awaits it before querying). **No
`database.py` changes required.**

```sql
-- Freeform category tree, unlimited depth. A node may contain
-- subcategories AND/OR templates.
CREATE TABLE IF NOT EXISTS minigame_categories (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    parent_id            INTEGER,          -- NULL = root; -> minigame_categories.id
    name                 TEXT NOT NULL,
    weight               INTEGER NOT NULL DEFAULT 1,
    sort_order           INTEGER NOT NULL DEFAULT 0,
    emoji                TEXT,             -- optional, UI only
    color                TEXT,             -- optional, UI only
    default_rewards_json TEXT,             -- JSON [{type, value, duration_hours, weight}]
    enabled              INTEGER NOT NULL DEFAULT 1,   -- D10: branch rotation-eligibility
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mgc_guild  ON minigame_categories(guild_id);
CREATE INDEX IF NOT EXISTS idx_mgc_parent ON minigame_categories(guild_id, parent_id);

CREATE TABLE IF NOT EXISTS minigame_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    category_id   INTEGER NOT NULL,        -- -> minigame_categories.id
    name          TEXT NOT NULL,
    game_type     TEXT NOT NULL,           -- quick_click|wheel|math|colors|emoji|rps
    enabled       INTEGER NOT NULL DEFAULT 1,   -- D9 master switch
    auto_spawn    INTEGER NOT NULL DEFAULT 1,   -- D9 rotation eligibility
    embed_json    TEXT NOT NULL,           -- Discord-API embed object (admin-designed)
    config_json   TEXT NOT NULL DEFAULT '{}',   -- per-type settings (§6)
    channel_id    INTEGER,                 -- NULL = guild default spawn channel
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mgt_guild_cat ON minigame_templates(guild_id, category_id);

-- Reward pool rows. No slot limit. Weights RELATIVE. ZERO ROWS IS VALID (D11).
CREATE TABLE IF NOT EXISTS minigame_rewards (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id    INTEGER NOT NULL,       -- -> minigame_templates.id (ON DELETE CASCADE)
    sort_order     INTEGER NOT NULL DEFAULT 0,
    reward_type    TEXT NOT NULL,          -- xp|coins|diamonds|item|role|temp_role
    reward_value   TEXT NOT NULL,          -- amount (xp/coins/diamonds) | item name | role id
    duration_hours INTEGER,                -- temp_role only
    weight         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mgr_template ON minigame_rewards(template_id);

-- Shuffle-bag state. Written ONLY by the bot process. Persists across restarts.
CREATE TABLE IF NOT EXISTS minigame_bag_state (
    guild_id     INTEGER NOT NULL,
    category_id  INTEGER NOT NULL,
    bag_json     TEXT NOT NULL,            -- JSON array of template ids remaining
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, category_id)
);

-- Dashboard-triggered spawns (test / manual force of a specific template).
CREATE TABLE IF NOT EXISTS minigame_spawn_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    template_id   INTEGER NOT NULL,
    mode          TEXT NOT NULL,           -- manual | test
    requested_by  TEXT,
    requested_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at  TIMESTAMP,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending|processing|done|failed
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_msr_pending ON minigame_spawn_requests(status, guild_id);
```

Extended `minigames_log` (the existing table is extended, **not** duplicated):

```sql
ALTER TABLE minigames_log ADD COLUMN template_id       INTEGER;
ALTER TABLE minigames_log ADD COLUMN template_name     TEXT;    -- snapshot at run time
ALTER TABLE minigames_log ADD COLUMN category_id       INTEGER;
ALTER TABLE minigames_log ADD COLUMN category_name     TEXT;    -- snapshot at run time
ALTER TABLE minigames_log ADD COLUMN game_type         TEXT;
ALTER TABLE minigames_log ADD COLUMN mode              TEXT DEFAULT 'auto';  -- auto|manual|test
ALTER TABLE minigames_log ADD COLUMN participants_json TEXT;    -- [{id, display_name}]
ALTER TABLE minigames_log ADD COLUMN winners_json      TEXT;    -- [{id, name, reward_type,
                                                                --   reward_value, status}]
ALTER TABLE minigames_log ADD COLUMN started_at        TIMESTAMP;
ALTER TABLE minigames_log ADD COLUMN ended_at          TIMESTAMP;
ALTER TABLE minigames_log ADD COLUMN status            TEXT DEFAULT 'completed';
                                                            -- completed|no_winner|aborted_restart
```

* Empty pool (D11): `winners_json` entries carry `reward_type: null,
  reward_value: null, status: "no_reward"` — the run row is the record that no
  rewards existed. No extra column needed.
* Legacy columns (`tier`, `winner_id`, `winner_display_name`, `forced`) stay;
  v1 rows remain untouched history. Names snapshotted → history survives any
  rename or delete.

`minigames_config` unchanged (weekly pacing + channel) plus:

```sql
ALTER TABLE minigames_config ADD COLUMN global_default_rewards_json TEXT;
```

(`claim_seconds` stays as a legacy, hidden, unused column.)

---

## 3. Migration and rollback strategy

1. **Timing:** new `ensure_tables()` runs at bot `cog_load` and is awaited by
   the dashboard before any minigames query — first run of the new code, either
   process. Every step idempotent.
2. **Steps:** create new tables → guard-check existing columns via
   `PRAGMA table_info` before each `ALTER` (safe on re-runs) → **one-time
   tier→category migration**: for each guild where `minigame_categories` is
   empty and `minigames_tiers` has rows, create one root category per tier
   (`name` = tier title, same `weight`/`enabled`), with that tier's single
   reward as a one-row `default_rewards_json` preset.
3. **Retirement, same deploy:** tier slash commands, `MinigameClaimView`,
   `VALID_TIERS*` constants, dashboard tier tab, `/minigames/tier*` routes.
4. **Rollback:** `minigames_tiers` table is **kept** (never dropped); redeploying
   the old code finds its data intact; new tables are simply ignored by it. No
   v1 data is lost at any point.
5. **Verification gate:** on a copy of the production DB: run migration twice
   (idempotency), diff tier data vs created categories, confirm v1 log rows
   render, then deploy.

---

## 4. Category tree & recursive selection algorithm

**Eligibility** (for automatic selection only — D10, D12):

```
playable(t)      = t.enabled AND t.auto_spawn
eligible(node)   = node.enabled
                   AND every ancestor enabled
                   AND ≥1 playable template in node's SUBTREE (direct or below)
```

**Recursive weighted traversal:**

```
select(virtual root):
    options = each eligible ROOT category        (weight = category.weight)
select(node):
    options =
      [ node's direct playable templates as ONE bag-option
        (weight = node.weight) ]                        — only if ≥1 exists
      + [ each eligible subcategory S
         (weight = S.weight) ]
    pick = weighted_random(options)
    if pick == bag   → pop node's shuffle bag → template
    if pick == sub S → return select(S)
```

One rule, plainly: *at any node, its direct games (as a group) compete with its
subcategories — the group carries the node's weight, each subcategory carries
its own.* A node with a single option simply takes it; pure organizational
chains pass through at 100%. Empty nodes/subtrees are **never candidates — they
cannot consume a selection** (round-3 requirement).

### Worked example A — mixed node (direct templates + subcategory)

```
Root
├── Bronze (w=50)
│   ├── Quick Click #1          ┐ direct bag (option weight = 50)
│   ├── Quick Click #2          ┘
│   └── Button Games (w=30)     ┐ subcategory option (weight = 30)
│       ├── Quick Click #3      │
│       └── Math #1             ┘
└── Silver (w=30)
    └── Math #2 (direct bag)
```

| Template | Probability |
|---|---|
| Quick Click #1 | 50/80 × 50/80 / 2 = **19.53%** |
| Quick Click #2 | **19.53%** |
| Quick Click #3 | 50/80 × 30/80 / 2 = **11.72%** |
| Math #1 (in Button Games) | **11.72%** |
| Math #2 (Silver) | 30/80 = **37.50%** |

(All five templates reachable — including ones living only in a subcategory.)

### Worked example B — pure organizational chain

```
Root
└── Events (w=1, no direct templates)
    └── Friday Games (w=1, no direct templates)
        ├── Math #1
        ├── Math #2
        └── RPS #1
```

Only one eligible root (100%) → only one eligible child (100%) → the leaf node's
only option is its direct bag → **Math #1 = Math #2 = RPS #1 = 33.33% each.**
Weights on organizational-only nodes are irrelevant (single path) — you may
nest freely without doing any weight math.

### Worked example C — disabled branch, empty category, no wasted selections

```
Root
├── Bronze (w=50, enabled)
│   ├── QC #1
│   └── "Later" (sub, EMPTY — no templates)
├── Silver (w=30, DISABLED)  ← 40 templates inside, all untouched
└── Gold (w=20, enabled, no templates anywhere below)
```

Eligible roots: **Bronze only** (Silver's branch is excluded while disabled even
though it holds 40 playable templates; Gold has nothing below → not a
candidate). Every automatic spawn therefore comes from Bronze's direct bag
("Later" never participates). Re-enabling Silver restores the 50/30 split with
all 40 templates intact — nothing was deleted, modified, or permanently
disabled (D10).

### Manual spawn (no template given)

`/minigames_spawn` without `template_id` runs the same recursive eligibility +
traversal + bag pop, with `mode='manual'` and no counter increment (D5, D12).

---

## 5. Shuffle Bag behavior & persistence

* One bag **per node**, holding that node's **direct** playable templates.
  Stored in `minigame_bag_state` (DB, not memory) — **survives bot restarts**;
  the bot process is the only writer (§19).
* Pop **without replacement**; when empty → reshuffle the full eligible list
  into a fresh bag. 50 templates ⇒ 50 distinct games before any repeat (D8).
* **Staleness guard:** before every pop, if the stored bag contains ids that are
  no longer playable (deleted / `enabled=0` / `auto_spawn=0`), the bag is
  rebuilt first. A deleted/disabled template can never be spawned, and can never
  clog a bag.
* Bags are per-node: siblings, parents, and children each have independent bags
  (round-1 decision, unchanged).

---

## 6. Game engines — exact resolution & timeout behavior

Shared base (`utils/minigame_engine.py`):

```
MinigameEngine(snapshot, mode, log_id)
  start(channel)      → post message + view, arm timers
  on_interaction      → per type, guarded by self.finished (late clicks ignored)
  finalize(winners, note) →
      1. compute final embed text + button states
      2. per winner: independent reward roll (§7) → give_reward()
         (RewardError caught per winner → recorded; others continue)
      3. edit original message (best-effort; a failed edit never loses rewards)
      4. close log row (winners_json, participants_json, ended_at, status)
  cleanup             → cancel pending timers, discard views
```

custom_ids are run-scoped (`mg_{type}_{log_id}_…`). **No-forfeit principle (D3):**
every timeout ends `no winner, no reward` unless a valid winning state exists.

### 6.1 Quick Click / Reflex
| Aspect | Behavior |
|---|---|
| Start | Admin's embed + N numbered buttons `1 2 3 4 5` (D7), **all disabled** (visually invalid; no "too early" spam) |
| Reveal | After `random.uniform(min_delay, max_delay)` seconds: **one random-position** (D2) button → enabled + green (SUCCESS); others stay disabled |
| Winning | First user to press the green button |
| Timeout | Post-reveal wait expires, no press → **no winner, no reward**, "no one made it in time" |
| Final embed | Winner (or no-winner note) appended; all buttons disabled; footer end time |

### 6.2 Wheel
| Aspect | Behavior |
|---|---|
| Start | Admin's embed + one **Join** button |
| Participation | Click = join (ephemeral "You're in!"; re-click → "already in"); no live counts |
| End of window | 0 joined → no winner. ≥1 joined → **exactly one** uniform-random winner |
| Final embed | "🎡 **{winner}** wins the wheel — N participants" / "no one joined" |

### 6.3 Multiple Choice — Math / Colors / Emoji (one engine, three template types)
| Aspect | Behavior |
|---|---|
| Start | Admin's embed + one button per answer (2–6), **labels exactly as typed** (D6, e.g. `🔵 Blue`, `105`) |
| Participation | Any click records/overwrites that user's pick → **last answer before expiry counts** (ephemeral "Answer set — you can change it until time runs out") |
| During game | **No vote counts, no participant list, no correct-answer reveal** |
| End | Correct button green, wrong buttons red (DANGER), all disabled; correct answer revealed in embed |
| Winners | **Everyone** whose final answer was correct — all win |
| Nobody correct | No rewards; correct answer revealed; "no one got it right" shown clearly |
| Final embed | Correct answer + winner names, or the no-winner note |

### 6.4 RPS
| Aspect | Behavior |
|---|---|
| Start | Admin's embed + **Join** button |
| Seating | First 2 clickers seated as P1/P2; Join disabled; "2/2 seated" |
| Seating timeout | Dashboard-configurable (default 60s). <2 seated → **no winner, no reward** |
| Choosing | Both seated → each receives a **private ephemeral message with their own Rock/Paper/Scissors buttons** (Discord cannot user-target shared-message buttons); main message shows "⏳ awaiting choices" |
| Choice timeout | Dashboard-configurable (default 60s). **Resolves immediately once both chose**; expiry before both (including 1-of-2) → **no winner, no reward** — never a forfeit win (D3). A seated player who disconnects loses the ephemeral → covered by the same timeout |
| Resolution | Standard matrix; **draw → no reward** |
| Final embed | Player 1 + choice, Player 2 + choice, result: winner or "draw — no reward" |

---

## 7. Reward system behavior (including empty pools)

* **v1 types:** `xp`, `coins`, `diamonds`, `item`, `role`, `temp_role`
  (`duration_hours`). Vouchers/coupons = v2.
* **Pool:** per-template rows; **no slot limit**; weights relative (dashboard
  shows computed %; sum ≠ 100 allowed and unmarked).
* **Empty pool (D11) — valid configuration:**
  * The game runs exactly like any other game.
  * Winners are still determined and shown in the final embed.
  * **No rewards are granted** (the roll step is skipped, not an error).
  * The log records it: each winner entry has `reward_type: null,
    reward_value: null, status: "no_reward"`; the History UI shows a
    "no reward pool" badge on the run.
  * Saving with an empty pool is never blocked, and no UI warning treats it as
    an error (an informational note "This game has no reward pool — winners
    will be announced only" is acceptable UX, nothing more).
* **Roll (non-empty pool):** each winner gets an **independent**
  `random.choices(rows, weights)` roll — different winners can get different
  rewards or the same one; both correct by design.
* **Delivery:** exclusively `utils.reward_engine.give_reward()` (economy-safe
  credits, xp/level hooks, temp-role scheduling, item catalog, role-position
  checks). No new grant path.
* **Partial failure:** one winner's grant fails → others continue; failure
  recorded in `winners_json` (`status:"failed"` + error) + bot log. No retry
  queue in v1.
* **Defaults (never restrictions):** creation prefill order = template's own
  rows → category `default_rewards_json` → `global_default_rewards_json`.
  After save the pool is the template's own; it can be edited, emptied, or
  refilled freely at any time.

---

## 8. Automatic spawn flow

The 30-minute `daily_check_loop` and its adaptive weekly probability are kept
verbatim (D5: this decides only *whether* an automatic spawn happens).

```
weekly roll succeeds (or Sunday force-fire)
  → select_and_spawn(guild):
      1. eligibility per §4 (node, ancestors, subtree)
      2. recursive weighted traversal → template (or nothing)
      3. SNAPSHOT: template + embed_json + config_json + reward rows
         (names also snapshotted into the log row at start)
      4. channel = template.channel_id or config.channel_id; validate
      5. spawn_game(guild, snapshot, mode="auto")          (shared, §8.5 below)
      6. events_this_week += 1   — only after the message posts successfully
                                   (matches current behavior; D5)
  → if step 2 found nothing (no eligible root category):
      nothing spawns, counter NOT incremented, reason printed,
      retried at the next daily check   (mirrors current "no tiers" behavior)
```

### Slash commands
* `/minigames_setup` — kept (channel + weekly range).
* `/minigames_spawn [template_id]` — kept; with `template_id` = force exactly
  that template (manual, D12); without = §4 manual path.
* `/minigames_tier_add|list|remove` — **retired** (categories are
  dashboard-managed).

---

## 9. Manual spawn & Test Spawn flow

Both are **dashboard or slash → DB request row → bot executes** (the dashboard
cannot run a live engine — §1). One shared path, real engine, no fake
implementation (D4).

```
admin clicks [Test Spawn] (tree row / builder) or [Force spawn]
  → POST /minigames/templates/<id>/spawn {mode}
      permission: LEVEL_ADMIN; guild from session; template must belong to guild
      D12 check: test → always; manual → template.enabled
      duplicate pending request for the same template → rejected ("already queued")
      log_action audit entry
  → row inserted in minigame_spawn_requests (status=pending)
  → UI: "queued" toast (test) / "spawning" (manual)
bot spawn_request_loop (every 10s)
  → atomic claim: UPDATE ... SET status='processing' WHERE id=? AND status='pending'
    (rowcount check — idempotent even if the loop ever ran twice)
  → load template (fresh read, then snapshot) → spawn_game(mode)
  → row: done (+ message id into the run log) or failed(+error)
  → UI status pollable via GET /minigames/spawn-requests
```

* **Test:** `[Test]` prefix in the embed title, `mode='test'` in history,
  real engine/buttons/timers/rewards (empty pool ⇒ announcement only, D11),
  **never** touches the weekly counter or probability (D5).
* **Manual (specific template):** `mode='manual'`, no counter, no `[Test]`
  mark. Works even when the containing category is disabled (D10) and even
  when `auto_spawn=0` — as long as `enabled=1` (D12).

---

## 10. Dashboard / API architecture

Existing patterns only: Flask page route + `@require_page`, Jinja under
`dashboard/templates/systems/`, vanilla JS, shared `api_bp` blueprint,
`ajaxSave`/`showToast`/`setLoading`/`showConfirm`, CSRF-patched fetch.
**No new framework, no SPA.**

### API routes (all guild-scoped via session; `@require_api_permission(LEVEL_ADMIN)`)

| Method & path | Purpose |
|---|---|
| `GET  /minigames/categories` | Full nested tree; per node: id, name, weight, enabled, subtree-eligibility flag, direct/active template counts, preset summary |
| `POST /minigames/categories` | `{name, parent_id?, weight?, emoji?, color?, default_rewards?}` |
| `PATCH /minigames/categories/<id>` | Rename / weight / move / emoji / color / preset / enabled — rename safe (id references); `log_action` |
| `DELETE /minigames/categories/<id>` | **409 if it has direct templates or subcategories** (move/delete them first) |
| `GET  /minigames/templates?category_id=&include_disabled=1` | List (+ subtree filter), reward summary (incl. "no pool"), live-run flag |
| `POST /minigames/templates` | Create; rewards optional (empty pool valid, D11); omitted → prefill chain (§7) |
| `GET  /minigames/templates/<id>` | Full row for the builder |
| `PATCH /minigames/templates/<id>` | Update; rewards[] replaced atomically (delete + reinsert, one transaction) |
| `DELETE /minigames/templates/<id>` | Safe while a run is in progress (snapshot, §14) |
| `POST /minigames/templates/<id>/duplicate` | Deep copy; trailing `#NN` auto-incremented if present else ` (copy)`; returns new id |
| `POST /minigames/templates/<id>/spawn` | `{mode:"manual"|"test"}` → §9 queue flow |
| `GET  /minigames/spawn-requests?status=pending` | UI status poll |
| `GET  /minigames/history?limit=50&category_id=` | Extended log (legacy v1 rows rendered as-is) |
| `GET/POST /minigames/config` | Existing + `global_default_rewards` |

**Retired:** `GET /minigames/tiers`, `POST /minigames/tier`, `DELETE
/minigames/tier/<id>`.

### Pages
* `systems/minigames.html` — rebuilt, same URL; three client-side tabs, zero
  reloads: **Config** (weekly min/max, channel, enabled, global default preset
  editor) · **Categories & Templates** (unified tree, breadcrumbs, node/template
  quick actions, live-run badges) · **History** (mode badges, participants
  expandable, winners + rewards, "no reward pool" badge, legacy section).
* `systems/minigame_builder.html` — **new**, one route in `dashboard/app.py`
  with the same `@require_page` key as the current minigames page.
* `static/js/embed-composer.js` — **new shared component**: the per-embed
  editor (title, description, color picker+hex, author, image, thumbnail,
  footer, fields) + live preview renderer, **extracted** from
  `manage/embedbuilder.html`; the old page is refactored to consume it and
  regression-tested (§20). Fallback if extraction proves risky mid-implementation:
  duplicate the minimal single-embed editor inside the builder page only —
  reported before merge (internal refactor, no agreed-behavior change).

---

## 11. Builder creation flow

### 11.1 The 19-step flow (agreed)

```
1  Open a Category (tree tab)                       10 Configure the game timer
2  Click [+ Template]  → builder opens              11 Configure the reward pool
3  Give it a name                                   12 Choose the Category
4  Choose the game type (6 cards)                   13 Choose the target channel
5  Configure the mechanics                          14 Decide automatic rotation
6  Build the Embed                                  15 Preview the complete message
7  Add image/title/description/footer etc.          16 See the actual game buttons
8  Configure the answers/options                    17 Save
9  Select the correct answer                        18 Test Spawn
                                                  19 Enable it for the automatic system
```

Left column = one continuous scroll, exact order:
**Identity → Game Type → Game Settings → Embed → Rewards → Spawn Settings**.
No sub-pages, no tabs that can lose state. The admin should feel like they are
**building a game**, not configuring a backend.

### 11.2 Per-type fields (Game Settings section)
| Type | Fields |
|---|---|
| Quick Click | button count (2–6), reveal delay min (s), reveal delay max (s), wait after reveal (s) |
| Wheel | join window (s) |
| Math | question text, answers A–F (2–6, exact label text), correct answer, answer time (s) |
| Colors | image URL (required), optional question text, answers A–F, correct, answer time |
| Emoji | question text, optional image, answers A–F, correct, answer time |
| RPS | seating timeout (s, default 60), choice timeout (s, default 60) — both dashboard-configurable |

### 11.3 Spawn Settings section
Category (pre-filled with the one opened from), target channel
(`nero-channel-picker`, default = guild spawn channel, per-template override
allowed), and the two toggles (§13).

---

## 12. Live Preview behavior

* Right pane, **sticky** while the left column scrolls; below ~1100px it
  stacks under the form (responsive).
* Renders **the actual final Discord message**: the admin's embed **plus the
  interactive component row the engine will generate** for the selected type —
  answer buttons (exact typed labels), Join button, numbered Quick Click
  buttons, RPS — so embed + components read as **one complete game**, not two
  systems.
* Re-renders on every input (debounced ~200ms): embed fields, per-type content
  (question/answers/image), type switch, button count, answer count.
* The correct answer is **never** visible in the preview.
* Reuses the extracted embed composer's renderer (§10) — identical look to the
  existing Embed Builder preview.

---

## 13. Enabled / Auto-Rotation / Category-disable behavior

**Template (D9) — exact UI copy:**
```
Game Status
────────────────────────
Enabled            [ ON ]
   "Allows this game to be used normally."
Automatic Rotation [ ON ]
   "Allows the automatic spawn system to select this game."
```

| Enabled | Auto-rotation | Automatic | Manual (specific) | Test |
|---|---|---|---|---|
| ✓ | ✓ | ✓ | ✓ | ✓ |
| ✓ | ✗ | ✗ | ✓ | ✓ |
| ✗ | ✗ | ✗ | ✗ | ✓ |

The `Enabled ✓ / Rotation ✗` row **is** the "build → test → enable later" flow
(step 19): flip the toggle from the tree row, no builder needed.

**Category (D10) — exact UI copy:**
```
Category
────────
Enabled  [ ON ]
   "Controls whether this category branch is eligible for automatic rotation.
    Disabling excludes everything inside the branch from automatic rotation.
    Nothing is deleted or modified — re-enabling restores eligibility."
```

* Disabling a category: no deletions, no DB modifications, no template changes,
  no permanent disabling — **pure branch exclusion from automatic rotation**,
  applied recursively through all nested levels. Re-enable → normal recursive
  selection resumes per each template's own settings.
* Manual/Test spawning of templates inside a disabled category follows the
  template's own toggles only (D12).
* Tree UI makes state legible at a glance: disabled branch rendered dimmed
  with an "off rotation" badge; per-template badges for rotation-off and
  live-run.

---

## 14. Snapshot behavior for running games

* At spawn the live game receives a **deep copy**: template row, embed_json,
  config_json, reward rows (+ names for the log). After start, **nothing reads
  the template again** — editing or deleting the template mid-game cannot affect
  the running game; a deleted template's running game finishes normally.
* The log row stores `template_name` / `category_name` snapshots → history
  survives renames and deletes.
* **Bot restart mid-game (known limitation, handled):** in-memory views die
  with the process. Startup sweep: log rows with `ended_at IS NULL` older than
  5 minutes → finalize as `status='aborted_restart'`, no rewards, best-effort
  embed edit ("⚠️ Game ended — bot restarted"). Documented, never silent.
* **Cleanup after every finalize/abort:** pending timers cancelled, views
  discarded, bag state persisted, log row closed. No run survives on state
  other than its log row.

---

## 15. Logging & History

* Single source: extended `minigames_log` (§2) — **no parallel log tables**.
* Every run row answers: what game (name/type snapshot) · what category
  (snapshot) · when (started/ended) · auto/manual/test · who participated ·
  who won · what each winner received (type + value + success / failed /
  no_reward) · status (completed / no_winner / aborted_restart).
* v1 rows (tier-based claim events) remain, rendered in a legacy format.
* `log_action` (existing audit helper) records admin CRUD: category/template
  create, rename, move, delete, duplicate, spawn requests.

---

## 16. Security & permission boundaries

* **AuthZ:** every new API route `@require_api_permission(LEVEL_ADMIN)`
  (matches existing minigames routes); builder page uses the same
  `@require_page` key as the current minigames page.
* **Guild scoping / IDOR:** guild always resolved from the session
  (`get_session_guild_id()`); **no client-supplied guild id anywhere**;
  template/category ids validated against the session guild before any
  operation.
* **CSRF:** app-level CSRF on all `/api` non-GET routes inherited
  automatically (existing mechanism).
* **Attack surface:** the dashboard never needs the Discord token for this
  feature — spawn execution happens in the bot via the request queue (§1, §9);
  the existing REST-direct pattern is not extended.
* **Input validation:** server-side whitelist/limit validation (§17) mirrored
  client-side; embed URLs restricted to `http(s)`; all user content rendered
  through the existing `_escapeHtml` helper.
* **Abuse guards:** one pending spawn request per template (duplicates
  rejected); atomic claim on the poller (§19); per-guild queue bounded in
  practice by the 10s poller.
* **Audit:** `log_action` on create/rename/move/delete/duplicate/spawn-request.
* **Destructive actions:** category delete 409s while populated; all deletes
  require `showConfirm` in the UI.

---

## 17. Validation & edge cases

**Validation (server-side, mirrored client-side):**
category/template names non-empty + length caps; `game_type` whitelist; embed
fields within Discord limits (title ≤256, description ≤4096, footer ≤2048,
fields ≤25 with name ≤256/value ≤1024, image/thumbnail `http(s)` URLs only);
per-type numeric ranges (delays/times 1–300s, button/answer counts 2–6,
correct-index in range); reward rows: type whitelist, integer weight ≥1,
positive `duration_hours` for temp roles — **zero rows is valid (D11)**;
category `weight` ≥ 1; `parent_id` must be an existing same-guild category
(cycle-checked: a node cannot become its own descendant).

**Edge-case matrix:**

| Edge case | Behavior |
|---|---|
| Empty category / branch (nothing playable below) | Never a candidate at any level — cannot consume a selection |
| Games living only in subcategories | Fully reachable (recursive traversal, D1) |
| Disabled category branch | Excluded from automatic rotation only; manual/test follow template toggles (D10/D12); nothing modified |
| No eligible templates at all | Auto: nothing spawns, counter NOT incremented, reason printed, retried next daily check |
| Renamed category | Templates unaffected (id references); history keeps old name (snapshot) |
| Editing a template while its game runs | Running game unchanged (snapshot); change applies to future spawns |
| Deleting a template while its game runs | Game finishes normally; bag staleness guard rebuilds on next pop |
| Bot restart during a game | Startup sweep → `aborted_restart`, no rewards, embed note |
| Missing/deleted channel (auto) | Counter not incremented, reason printed, retried next daily check |
| Missing/deleted channel (manual/test) | Request row `failed` + error surfaced in dashboard |
| Template channel override deleted, default valid | Falls back to guild default spawn channel |
| No participants (any type) | Game ends, no winner, no reward, final embed states it clearly |
| Multiple winners (MC) | All correct answers win; each rolls independently; duplicates allowed |
| Wheel with 1 participant | That participant wins (100%) |
| No correct answers (MC) | No rewards; correct answer revealed; "no one got it right" shown |
| RPS draw | No reward; draw shown |
| RPS: 1 of 2 chose, other doesn't (timeout) | Ends, **no winner, no reward** (no forfeit, D3) |
| RPS: seated player disconnects | Ephemeral gone → choice timeout → no winner, no reward |
| Empty reward pool (D11) | Valid: game runs, winners shown, nothing granted, log `status:"no_reward"` |
| Failed reward grant (role above bot, deleted item…) | Other winners continue; failure in `winners_json` + bot log |
| Failed final embed edit | Rewards already delivered; failure logged; run still closes |
| Late/duplicate clicks after end | `self.finished` guard — ignored |
| Weekly min/max limits | Untouched v1 pacing; only successful automatic posts increment the counter (D5) |
| Manual spawn (dashboard or slash) | Real engine, `mode='manual'`, no counter, §4/D12 rules |
| Test spawn | Same as manual + `[Test]` + `mode='test'`; duplicate pending rejected |

---

## 18. Failure handling

Deliberate ordering on every finalization: **resolve winners → grant rewards →
edit embed (best effort) → close log.** A failed embed edit never loses
rewards; a failed grant never stops the others. Every timeout path funnels
through the same `finalize(winners=[], note)` no-forfeit path (D3). No
automatic retry of failed sends/grants in v1 (kept visible in logs instead).

---

## 19. Concurrency behavior

* **Two processes, one DB:** already safe — `PRAGMA journal_mode=WAL` +
  `busy_timeout=5000` set in `database.py` (readers never block writers).
* **Write ownership per table (no contention by construction):**
  `minigame_categories` / `minigame_templates` / `minigame_rewards` /
  `minigame_spawn_requests` (writes) → **dashboard only**
  `minigame_bag_state` / `minigames_log` (run rows) / `minigame_spawn_requests`
  (claim/mark) / `minigames_config` (counter) → **bot only**
  The dashboard reads everything for display; the bot reads config/CRUD.
* **Live games:** owned entirely by the bot process (its websocket delivers the
  interactions) → no cross-process race on a running game; `self.finished`
  guards late/duplicate clicks within the process.
* **Request-queue claim:** atomic
  `UPDATE … SET status='processing' WHERE id=? AND status='pending'` with
  rowcount check — idempotent even if the poller ever double-fires.
* **Restart-sweep idempotency:** sweep only touches rows with
  `ended_at IS NULL`; double-finalize impossible.
* **Bag writes:** single writer (bot), one row per (guild, category);
  rebuild-before-pop guard handles any staleness.
* **Dashboard double-submit:** `setLoading`/`ajaxSave` disable the button
  during the request; reward-row replacement is one transaction.

---

## 20. Testing strategy & major scenarios

**Logic (scriptable, no Discord):**
* Recursive selection against the three §4 worked examples: assert exact
  per-template probabilities (10k-draw Monte Carlo within tolerance),
  organizational pass-through, disabled-branch exclusion, no wasted selections
  on empty nodes.
* Shuffle bag: 2×N draws over N templates → no repeats until exhaustion,
  exactly one reshuffle; stale-id rebuild; per-node isolation.
* Reward roll: 10k rolls vs weights; duplicates allowed; empty pool → no
  grants, winners still recorded.
* Last-answer-wins override; RPS matrix incl. draw, 0-seated, 1-seated,
  1-of-2-chose timeouts.
* Counter accounting: auto increments only on successful post; manual/test
  never; failed-selection day consumes nothing.
* Cycle check: a category cannot be moved under its own descendant.

**Live lifecycle (test guild, per game type):** automatic spawn · manual force
of a specific template · test spawn (queue latency, `[Test]` mark, log) ·
zero participation · every timeout · multi-winner + same-reward ·
one-failed-reward isolation · **empty-pool game end-to-end (D11)** ·
edit-template-mid-run · delete-template-mid-run · bot restart mid-run (sweep) ·
channel deleted (auto + manual paths) · category rename → templates intact ·
category disable → branch excluded, manual still works, re-enable restores ·
category delete blocked while populated · duplicate → change content → save ·
history readable after template delete.

**Dashboard:** save → toast + no reload + breadcrumbs intact; double-click
Save = one row; simulated API 500 → form preserved + error toast; confirmations
on all deletes; test-spawn feedback + "already queued" guard; tree
expand/collapse/move; preview shows the correct component row per type (labels
exactly as typed; correct answer never visible); reward % live recompute; the
full 19-step flow performed verbatim once (target: new Math game from duplicate
≈ 30s); disabled-branch dimming + badges.

**Regression:** old Embed Builder page (load/save/send/preview) after composer
extraction; `/minigames_setup` + `/minigames_spawn` still work; v1 history rows
render; migration run twice on a production-DB copy (idempotency + data diff).

---

## 21. Exact implementation order & dependencies

```
Phase 1  utils/minigame_store.py
         tables + guarded ALTERs, tree queries, bag ops, request queue,
         snapshot reads, tier→category migration      deps: —
Phase 2  utils/minigame_engine.py
         base engine + finalize pipeline + 6 engines
         (MC engine shared by math/colors/emoji), reward rolls  deps: 1
Phase 3  cogs/minigames.py
         recursive selection, spawn_request_loop (10s), startup sweep,
         shared spawn_game(), command updates; retire tier code  deps: 1, 2
Phase 4  dashboard/api/minigames.py
         §10 routes + validation; retire tier routes            deps: 1
Phase 5  Dashboard frontend
         embed-composer extraction (+ old-page regression),
         minigames.html rebuild (tree/breadcrumbs/history),
         builder page + JS                                    deps: 4
Phase 6  Migration + retirement pass
         run on production-DB copy, verify, deploy with
         code retirement (tier commands/tab/routes)           deps: 1, 3, 5
Phase 7  Testing
         logic scripts → live lifecycle (all types) →
         dashboard flows → regression                         deps: all
```

Each phase ends with its verification gate (§20) before the next begins.

---

## 22. Remaining technical risks & ambiguities

**Risks (all with mitigations; none require a design change):**
1. *Embed-composer extraction* from the 1466-line page may surface unexpected
   coupling — mitigation: old-page regression suite after extraction; fallback
   = duplicate the minimal editor in the builder page only (internal, no
   agreed-behavior change; reported before merge).
2. *Bot restart mid-game* kills live views — accepted limitation, sweep in §14.
3. *Discord ephemeral lifetime* (RPS choices) — covered by the no-forfeit
   timeout rule (§6.4, D3).
4. *Test/Manual spawn latency ≈ 10s* — inherent to the DB-queue architecture
   (dashboard cannot run a live engine); accepted (D4).

**Ambiguities:** none. Every architectural, UX, and behavioral decision in
scope is locked in the decision log (§A). Per the round-3 rule, if anything
emerges during implementation that would touch architecture, security, data
integrity, user-facing behavior, or the agreed scope, work pauses and it is
flagged before the decision is made.

---

## C. End-to-end walkthrough — from zero to a player's reward

**1. Categories from scratch.** The admin opens the dashboard → Systems →
Minigames → Categories & Templates tab (empty). `[+ Category]` → **Bronze**
(weight 50) → sets a default reward preset (100 XP ×5, 50 Coins ×3, item
"Starter Pack" ×2) → save. Inside Bronze: `[+ Subcategory]` → **Button Games**
(weight 30). Tree now: `Minigames / Bronze / {Button Games}`.

**2. Building the first game.** Bronze → `[+ Template]`. Builder opens:
*Identity* — name it **"[Math] 15 × 7"**. *Game Type* — **🧮 Math**.
*Game Settings* — question `15 × 7 = ?`, answers `95 / 105 / 115 / 125`,
correct = `105`, time `30s`. *Embed* — the composer prefills the description
with the question; admin sets title **🧮 Math Challenge**, color, footer
"Answer before time runs out!". *Rewards* — prefilled from Bronze's preset
("Loaded from Bronze defaults — edit freely"); the admin leaves it.
*Spawn Settings* — category Bronze (pre-filled), channel = guild default,
**Enabled ✓, Automatic Rotation ✗ for now** (step 19 flow). The right pane has
been rendering the whole time: the embed with four buttons underneath —
exactly the message that will be posted.

**3. Save → test → enable.** Save → toast → back on the Bronze tree with
breadcrumbs. `[Test Spawn]` → "Test queued" → ~10s later
**[Test] 🧮 Math Challenge** appears in the spawn channel with its four
buttons. A member clicks `105` → ephemeral "Answer set — you can change it…".
At 30s: `105` turns green, the others red, the embed reveals
"✅ Correct: 105 — Winners: @member". The reward rolls (say 100 XP),
`give_reward` credits it through the normal ledger, the final embed is edited,
History gains a `test` row with participant, winner, and reward. The admin
flips **Automatic Rotation on** from the tree row — no builder needed.

**4. Building the library.** `[Duplicate]` → "[Math] 25 × 4" → change question
+ answers → save. Repeat — 50 Math games land in Bronze's direct list, a
handful of Quick Click games under Button Games, Colors/Emoji games in new
categories. Each new game ≈ 30 seconds.

**5. A no-reward game (D11).** One game is saved with an **empty reward pool**
(a pure fun game). When it later runs: buttons, timer, reveal — all normal;
winners are announced in the final embed; nothing is granted; the log row's
winner entries read `no_reward` and History shows the "no reward pool" badge.

**6. The automatic system picks it up.** Days pass. The weekly pacing loop
(min 5–10 events, adaptive daily probability, Sunday force-fire) decides a
spawn is due. `select_and_spawn` runs: Bronze (50) beats the other roots;
inside Bronze the direct bag (50) beats Button Games (30); Bronze's shuffle bag
yields template #17 — a game not seen since the bag was last shuffled.
Snapshot taken, channel resolved, message posted, weekly counter incremented.

**7. The player's experience.** A player sees 🧮 Math Challenge, reads the
question, clicks an answer (can change until the last second), watches the
reveal, and — if correct — their name appears in the winner list while the
reward roll lands in their balance/XP/inventory via the shared reward engine.
The log row closes with participants, winner, and reward; the bag now holds 49
games, so the next Bronze pick will be a different one.

**8. Organizing later.** The admin renames Bronze → "Weekly Games": templates,
weights, and presets all survive (id references), old history keeps the
original name. A disabled test category is dimmed in the tree with an "off
rotation" badge; re-enabling restores its branch instantly. A template that
aged out is deleted from its row — any in-progress run of it finishes
normally, the bag rebuilds on the next pop, and history remains readable.

---

*End of final plan. On sign-off, implementation proceeds exactly per §21.*
