# Currency System — Full Repository Audit

**Scope:** every currency-related path in `Nilive-Bot` — configuration, storage, display,
reward granting, dashboard, Discord commands, and the Missions system.
**Method:** static sweep of all `.py` / `.js` / `.html` / `.md` (excluding `node_modules`),
plus runtime verification against a scratch SQLite DB built by the project's own
`init_db()`, plus an AST pass over `cogs/events.py`, plus the three existing test suites.
**Status:** audit complete, plan revised for the UI requirements — **no implementation
started**, per instruction.

> **Revision 2** adds §13 (UI requirements: the animated check emoji and the neutral refresh
> button) and §14 (final repository-wide search), and folds both into a revised §10 plan.
> §13 contains one genuinely blocking finding — the check emoji is **not** a simple
> find-and-replace — so read §13.2 before approving Phase 4.

Baseline test results captured before any change:

| Suite | Result |
|---|---|
| `scripts/test_missions_v2.py` | 147 / 147 passed |
| `scripts/test_wallet_phase2.py` | 54 / 54 passed |
| `scripts/test_wallet.py` | 73 / 73 passed |

---

## 0. Headline findings (read this first)

| # | Finding | Severity |
|---|---|---|
| 1 | `Moon` **does not exist anywhere in this repository.** Not in code, not in the DB schema, not in any commit (the repo has exactly 2 commits of history). The defaults today are `Coins` / 🪙 and `Diamonds` / 💎. The requirement's `Moon` / `قمر` example is aspirational, not a rename of existing data. | context |
| 2 | **Saving General Settings is completely broken on any fresh database.** Both writers `INSERT ... currency_emoji_id`, a column that no longer exists. Verified: `OperationalError: table guild_settings has no column named currency_emoji_id`. Because it is a single `INSERT` covering prefix/timezone/log-channel/currency, **one dead column silently kills the entire form** — timezone and log-channel changes are lost too. | 🔴 critical |
| 3 | **Coin/diamond Events never launch.** `cogs/events.py:239` reads `interaction.guild.id` inside `_launch_event()`, which has no `interaction` parameter. AST-verified `NameError`, swallowed by a broad `except Exception` → printed to console, event silently never posted. | 🔴 critical |
| 4 | Currency **display** config and currency **storage identity** are already correctly separated (`balance`/`diamonds` keys in the DB, names/emoji for display only). **No database data migration is required to rename a currency.** | ✅ good news |
| 5 | Missions already resolve currency dynamically for the member display — but with hardcoded 🪙/💎 fallbacks, a dead hardcoded animated-diamond constant, and admin surfaces (slash commands, dashboard) that still print raw internal keys. | 🟡 partial |
| 6 | Two independent, duplicated General-Settings writers exist (`dashboard/app.py` and `dashboard/api/misc.py`). They have already drifted from the schema. | 🟠 design |

---

## 1. Current currency architecture

There are **two distinct layers**, and keeping them separate is the single most important
design constraint for this work:

### Layer A — Storage identity (internal keys). Must NOT be renamed.

The `economy` table has exactly two currency columns, and every system that records
"which currency" stores the **column name**, never a display name:

| Location | Column | Values |
|---|---|---|
| `database.py:156` | `economy.balance` | integer |
| `database.py:156` | `economy.diamonds` | integer |
| `database.py:1094` | `transaction_ledger.currency` | `'balance'` \| `'diamonds'` \| `'xp'` |
| `database.py:655` | `leveling_currency_rewards.currency` | `'balance'` \| `'diamonds'` |
| `database.py:978` | `item_catalog.value_currency` | `'balance'` \| `'diamonds'` |
| `purchase_history.currency_paid` | | `'balance'` \| `'diamonds'` |
| `missions_definitions.reward_type` | | `'coins'` \| `'diamonds'` \| `'xp'` \| `'role'` \| `'temp_role'` \| `'item'` |
| `events.reward_type`, `minigame_rewards.reward_type` | | same `'coins'`/`'diamonds'` keys |

Validated centrally in `utils/economy_safe.py:4` (`VALID_CURRENCIES = {"balance", "diamonds"}`)
and `utils/ledger.py:5` (`VALID_CURRENCIES = {"balance", "diamonds", "xp"}`).
All balance mutations go through `utils/economy_safe.py` (`safe_credit`, `safe_deduct`,
`safe_transfer`, `safe_admin_deduct`, `safe_convert`) → each writes a `transaction_ledger` row.

**Consequence: no stored row anywhere contains the string `"Coins"`, `"Diamonds"`, `"Moon"`,
or an emoji as a *value*.** Renaming is a pure presentation change. This is the strongest asset
the project has right now and must be preserved.

### Layer B — Display configuration (names + icons). This is what must move.

`utils/currency.py` (added in the Wallet pass) is the display helper. It reads four columns
from the **`guild_settings`** table:

```
utils/currency.py:52-53
    SELECT currency_name, coin_emoji_id, diamond_name, diamond_emoji_id
    FROM guild_settings WHERE guild_id = ?
```

and returns the shape every caller consumes:

```python
{
  "coins":    {"key": "balance",  "name": "...", "emoji": "..."},
  "diamonds": {"key": "diamonds", "name": "...", "emoji": "..."},
}
```

Defaults live at `utils/currency.py:26-29` (`Coins` / 🪙 / `Diamonds` / 💎). A missing
`guild_settings` row or a NULL/empty cell falls back to those defaults, so a fresh guild
always renders something sensible.

### The exchange rate — already "Economy-owned"

`guild_settings.diamond_exchange_rate` (default 500) is read/written **only** by the Economy
surface: `utils/economy_safe.get_guild_exchange_rate()` (`utils/economy_safe.py:290`),
`dashboard/api/economy_shop.py:102/118`, and `dashboard/app.py:1106` (`/economy` page).
This is the precedent to follow: economy-specific config already lives in `guild_settings`
but is **owned by the Economy module**. Currency names/emoji should follow the same pattern —
same table, different owner.

---

## 2. Where the current `Moon`/`Diamond` configuration lives

There is no `Moon`. The real configuration is spread across **five** locations that must be
reconciled:

| # | Location | What it holds | Owner today |
|---|---|---|---|
| 1 | `guild_settings.currency_name` (default `'Coins'`) | primary display name | **General Settings** |
| 2 | `guild_settings.coin_emoji_id` (default `'🪙'`) | primary icon | **General Settings** — ⚠️ **no UI writes this** |
| 3 | `guild_settings.diamond_name` (default `'Diamonds'`) | diamond display name | **nobody** — no UI at all |
| 4 | `guild_settings.diamond_emoji_id` (default `'💎'`) | diamond icon | **nobody** — no UI at all |
| 5 | `guild_settings.currency_emoji_id` | legacy column, **does not exist on fresh DBs** | General Settings UI still writes it |

The UI in `dashboard/templates/config/general.html:39-53` exposes only **two** of the four
real fields (`currency_name`, `currency_emoji_id`) and labels the primary currency
"Currency" — the diamond currency is not configurable from any surface at all.

### Defaults are duplicated in four places (drift risk)

```
utils/currency.py:26-29        DEFAULT_COIN_NAME / _EMOJI / DEFAULT_DIAMOND_NAME / _EMOJI
database.py:472-475            column DEFAULTs 'Coins' / '🪙' / 'Diamonds' / '💎'
database.py:504/508/512        ALTER TABLE ... DEFAULT (same values, again)
dashboard/templates/config/general.html:43,50   'Coins' / "Leave empty for default 🪙"
cogs/wallet.py:1034-1035       label_map / emoji_map literals
cogs/missions.py:133,136       '🪙' / '💎' fallbacks
cogs/events.py:241,245         {'emoji': '🪙', 'name': 'Coins'} fallback dicts
```

---

## 3. All files/systems that depend on currency configuration

### 3a. Consumers of `utils/currency.py` (18 call sites across 6 files)

| File | Sites | Usage |
|---|---|---|
| `cogs/economy.py` | 10 | `/balance`, `/give`, `/convert`, `/richest`, `/addcoins`, `/removecoins`, `/adddiamonds`, `/removediamonds` |
| `cogs/wallet.py` | 5 | hub embed, streak panel, claim receipt, receipts pagination, receipt line formatter |
| `cogs/shop.py` | 5 | price display, purchase receipt, insufficient-funds message, prestige reset message, `/shop` listing |
| `cogs/events.py` | 3 | event reward embed + claim message + **broken** `_launch_event` lookup |
| `cogs/missions.py` | 2 | `build_mission_display` config fetch, completed-reward line |
| `cogs/trade.py` | 2 | trade offer embed, insufficient-funds messages |

### 3b. Systems that render or move currency but bypass the helper

| System | File | Problem |
|---|---|---|
| Trade buttons | `cogs/trade.py:287,291` | `label="Offer Coins", emoji="🪙"` / `label="Offer Diamonds", emoji="💎"` hardcoded at class-definition time |
| Trade modals | `cogs/trade.py:69,97` | `title="Offer Coins"` / `title="Offer Diamonds"` hardcoded |
| Trade history | `cogs/trade.py:419,421` | `🪙{n:,}` / `💎{n:,}` hardcoded in the summary closure |
| Wallet receipts tabs | `cogs/wallet.py:1034-1035` | `label_map` / `emoji_map` duplicate the defaults (later overwritten at render — cosmetic flicker, but a second source of truth) |
| Dashboard members list | `dashboard/api/core.py:276` | `🪙 {r[3]:,}` hardcoded in HTMX partial |
| Dashboard economy leaderboard | `dashboard/api/economy_shop.py:61,97` | `🪙 {r[1]:,}` / `💎 {r[1]:,}` hardcoded |
| Dashboard shop table | `dashboard/api/economy_shop.py:177` | `💎 {n:,}` / `🪙 {n:,}` hardcoded |
| Dashboard purchase history | `dashboard/api/economy_shop.py:393` | `currency_icon = "💎" if ... else "🪙"` hardcoded |
| Dashboard leveling rewards | `dashboard/api/leveling.py:192` | returns raw key; template renders `💎 Diamonds` / `🪙 Coins` |
| Missions admin list/log | `cogs/missions.py:442,473`, `dashboard/templates/systems/missions.html:231,346` | prints raw `reward_type` key (`coins:` / `diamonds:`) instead of the configured name |

### 3c. Dashboard templates with hardcoded currency text/glyphs

| Template | Lines |
|---|---|
| `config/general.html` | 39, 43, 50 (the block to be **removed**) |
| `systems/economy.html` | 9, 22, 38, 52, 54-56, 61, 73, 76, 87, 90, 95 |
| `systems/leveling.html` | 244, 259-260, 327-329, 547, 683-685 |
| `systems/shop.html` | 35, 38, 41-44, 68-69, 162, 164, 360, 376, 380 |
| `systems/ledger.html` | 14-15, 22, 39, 52, 103, 115 |
| `systems/minigames.html` | 167 (`coins: '🪙 Coins', diamonds: '💎 Diamonds'`) |
| `systems/minigame_builder.html` | 156 (identical map) |
| `systems/missions.html` | 57-58, 231, 346 |
| `systems/trade.html` | 37-38 |
| `general/members.html` | 21, 33, 34 |
| `general/member_profile.html` | 22, 27, 60, 66, 151 |
| `base.html` | 474 (global edit-member modal: `Coins` label) |

### 3d. Non-issues (verified, no change needed)

* `utils/economy_safe.py` — pure key-based; **correct as-is**.
* `utils/ledger.py` — pure key-based; **correct as-is**.
* `utils/trade_engine.py:106` `_move_currency` — key-based; correct.
* `utils/inventory.py`, `utils/item_catalog.py` — `value_currency` is a key; correct.
* `utils/minigame_engine.py`, `utils/minigame_store.py` — reward delivery via
  `give_reward()`; keys only; correct.
* `utils/xp_calculator.py:288` — coerces to `balance`/`diamonds`; correct.
* `utils/rank_card_data.py:63-64` — key-based reads; correct.
* `utils/prestige.py` — mentions in **comments only**; behaviour is key-based.
* `cogs/boost.py:84,152` — comments only.
* `utils/daily_engine.py:42` — comment only; credit goes through `safe_credit`.

---

## 4. Hardcoded currency names / emojis

### 🪙 / 💎 literal inventory (production code)

```
cogs/events.py:241          {"emoji": "🪙", "name": "Coins"}      ← fallback dict
cogs/events.py:245          {"emoji": "💎", "name": "Diamonds"}   ← fallback dict
cogs/missions.py:133        "🪙"                                    ← fallback
cogs/missions.py:136        "💎"                                    ← fallback
cogs/trade.py:287           emoji="🪙"  label="Offer Coins"
cogs/trade.py:291           emoji="💎"  label="Offer Diamonds"
cogs/trade.py:419           f"🪙{offer['coins']:,}"
cogs/trade.py:421           f"💎{offer['diamonds']:,}"
cogs/wallet.py:1035         {"balance": "🪙", "diamonds": "💎"}
dashboard/api/core.py:276                  "🪙 {r[3]:,}"
dashboard/api/economy_shop.py:61           "🪙 {r[1]:,}"
dashboard/api/economy_shop.py:97           "💎 {r[1]:,}"
dashboard/api/economy_shop.py:177          "💎/🪙" ternary
dashboard/api/economy_shop.py:393          currency_icon ternary
```

### Hardcoded *name* strings in production code

```
cogs/wallet.py:1034         {"balance": "Coins", "diamonds": "Diamonds"}
utils/formatters.py:155     def format_coins(n, currency_name: str = "Coins")
dashboard/api/misc.py:95    data.get("currency_name", "Coins")
dashboard/app.py:1452       data.get("currency_name", "Coins")
```

### The most serious single hardcode

```python
# cogs/missions.py:62-66
# Single source of truth for the animated reward emoji (locked spec).
DIAMOND_EMOJI = "<a:diamond:1532745018324815982>"
CHECKMARK_EMOJI = "✅"
```

A **specific Discord animated emoji ID hardcoded inside the Missions module**, with a comment
declaring it "the single source of truth" — the exact opposite of the requirement. It is now
**dead code** (only `CHECKMARK_EMOJI` is still used, at `cogs/missions.py:160`), but it is
still asserted by `scripts/test_missions_v2.py:536-537`, so a test currently *guards* the
hardcode. This is both a rule-6 violation and a rule-10 violation.

### Dead currency helpers

* `utils/currency.py:87,91,95,99` — `coin_name()`, `coin_emoji()`, `diamond_name()`,
  `diamond_emoji()` are **defined but never called anywhere**.
* `cogs/trade.py:11` imports all four and uses none of them (lint-level noise, and it makes
  the file *look* config-driven when it is not).
* `utils/formatters.py:155` `format_coins()` is **never called** and carries a hardcoded
  `"Coins"` default.

---

## 5. How Missions currently define and grant rewards

### Definition (storage is clean)

`utils/mission_engine.py:86-101` — `missions_definitions`:

```sql
reward_type            TEXT NOT NULL,   -- 'coins' | 'diamonds' | 'xp' | 'role' | 'temp_role' | 'item'
reward_value           TEXT NOT NULL,   -- amount, role ID, or item name
reward_duration_hours  INTEGER          -- temp_role only
```

`utils/mission_engine.py:64` — `VALID_REWARD_TYPES` is **key-only**. No currency *name* is ever
stored. `_validate_reward_value()` (`:187`) requires a positive integer for `coins`/`diamonds`/`xp`.
Honoured by both entry points: `/mission_create` (`cogs/missions.py:401`) and
`POST /api/missions/definition` (`dashboard/api/missions.py:41`), both routing into
`create_definition()` — one validation path, no drift.

### Grant path (already dynamic at the logic layer) ✅

```
utils/mission_engine.py:479-494   record_activities()
        ↓
utils/reward_engine.py:35-...     give_reward(bot, guild_id, user_id, reward_type, amount=...)
        ↓  currency = "balance" if reward_type == "coins" else "diamonds"
utils/reward_engine.py:72         safe_credit(...) / safe_deduct(...)
        ↓
utils/economy_safe.py             economy.balance / economy.diamonds  →  transaction_ledger
```

`give_reward()` takes the **internal key**, never a display name. Currency *identity* in the
mission path is therefore already 100 % configuration-independent — rules 6, 7 and 9 are
already satisfied at the data layer. This is the part that must not regress.

Edge behaviour worth noting: prestige earn-multiplier lookup failure falls back to `mult = 1.0`
(`utils/reward_engine.py:66-70`), and a failed reward grant is logged, never raised
(`utils/mission_engine.py:491-498`).

### Display path (partially hardcoded) ⚠️

```
cogs/missions.py:191  build_mission_display()
cogs/missions.py:204-210  cur = await get_currency_config(guild.id)   ← correct: dynamic
cogs/missions.py:128-148  _reward_display_sync(m, cur)                ← correct shape,
                                                                        but 🪙/💎 fallbacks
cogs/missions.py:151-179  _mission_block()  →  "⤷ `reward claimed` <emoji> ✅"
```

Gaps:
1. `_reward_display_sync()` prints the **emoji only**, never the configured **name**
   (`f"{rv} {emoji}"`) — so a custom name is not shown on the member-facing completed line.
2. The `else "🪙"` / `else "💎"` fallbacks re-introduce a hardcoded currency icon whenever the
   config lookup raises (the `try/except` at `:206-210` sets `cur = None`).
3. Admin surfaces print the raw key: `/mission_create` reply (`:442`), `/mission_list`
   (`:473`), dashboard missions table (`missions.html:231`), completion log (`:346`).
4. `DIAMOND_EMOJI` dead constant (see §4).
5. `dashboard/api/missions.py` never loads currency config at all — it has no way to render a
   configured name even if the template wanted one.

---

## 6. What needs to move from General Settings → Economy

### Remove from General Settings

| Item | File | Action |
|---|---|---|
| Currency card (2 inputs) | `dashboard/templates/config/general.html:39-53` | delete the whole `<div class="card">` |
| `currency_name` read/write | `dashboard/api/misc.py:76,84,95` | remove from INSERT/UPDATE/params |
| `currency_emoji_id` read/write | `dashboard/api/misc.py:76,85,96` | remove (also fixes the 🔴 500) |
| `currency_name` read/write | `dashboard/app.py:1433,1441,1452` | remove |
| `currency_emoji_id` read/write | `dashboard/app.py:1433,1442,1453` | remove |

### Add to Economy

A new **Currency** tab on `/economy` (`dashboard/templates/systems/economy.html`, alongside
`coins` / `diamonds` / `exchange`) containing four fields:

| Field | Column | Type |
|---|---|---|
| Primary currency name | `currency_name` | text, blank ⇒ default |
| Primary currency emoji | `coin_emoji_id` | emoji ID / `<:name:id>` / `<a:name:id>` / unicode, blank ⇒ default |
| Diamond currency name | `diamond_name` | text, blank ⇒ default |
| Diamond currency emoji | `diamond_emoji_id` | as above |

With a live preview and a "Reset to defaults" affordance, mirroring the existing
exchange-rate card's layout (`economy.html:84-103`) so the page stays visually consistent.

### Storage decision (recommended: keep `guild_settings`)

Two options were evaluated:

* **Option A — keep the four columns in `guild_settings` (recommended).** Economically
  identical to the current exchange-rate precedent (`diamond_exchange_rate` already lives in
  `guild_settings` while being owned by the Economy page). Zero data migration, zero risk to
  the 18 existing reader call sites, `get_guild_exchange_rate()` untouched. Ownership moves
  via the *writer* and the *UI*, which is exactly what "Economy is the source of truth" means
  operationally.
* **Option B — new `economy_config` table.** Cleaner conceptual boundary, but requires a
  migration for every existing guild, a compatibility shim for the exchange rate, and rewrites
  of every reader. Higher risk, no functional gain, and it would split currency display config
  away from the exchange rate it is displayed alongside.

**Recommendation: Option A**, with the rule enforced in code — `utils/currency.py` becomes the
**only** module permitted to touch those four columns, and no other file may reference them.

---

## 7. Database / API changes required

### 7a. Database (all idempotent, all in the existing guarded-PRAGMA style)

| # | Change | Reason |
|---|---|---|
| D1 | **Fix the `currency_emoji_id` writers** (remove the column from both INSERTs) | 🔴 saves currently 500 on fresh DBs |
| D2 | One-time repair migration: if `currency_emoji_id` **exists** and `coin_emoji_id` is still the 🪙 default while `currency_emoji_id` holds a real value, copy the legacy value into `coin_emoji_id`, then leave the legacy column in place (never `DROP` on an already-deployed file) | preserves a legacy admin's configured icon instead of silently reverting it to 🪙 |
| D3 | Extend the existing `guild_settings` PRAGMA guard (`database.py:487-515`) to also self-heal `currency_name` if absent | completeness; `diamond_name`/`diamond_emoji_id`/`coin_emoji_id` already covered |
| D4 | No schema change to `economy`, `transaction_ledger`, `missions_definitions`, `leveling_currency_rewards`, `item_catalog`, `purchase_history`, `minigame_rewards`, or `events` | keys are already display-agnostic — **no data migration** |
| D5 | Optional, low priority: `utils/mission_engine.py` follows its own `ensure_tables()` convention — nothing currency-related is needed there | missions must not gain currency config (rule 10) |

### 7b. New / changed APIs

| Endpoint | Change |
|---|---|
| `POST /api/economy/currency` | **new** — validates + writes the 4 fields via `utils/currency.set_currency_config()`. Owner/Admin gated like `/api/economy/exchange-rate`. Logs through `log_action(..., "economy")`. |
| `GET /api/economy/currency` | **new** — returns current config + resolved defaults (for form prefill and preview). |
| `POST /api/settings/general` | **changed** — drop the two currency fields; keep prefix/timezone/log-channel/status-rotation; fixes the 500. |
| `POST /config/general` (`dashboard/app.py:1408`) | **changed** — same removal. Consider collapsing this duplicate writer into the `/api/settings/general` path, or at minimum keep both in sync. |
| `GET /economy` | **changed** — pass resolved `currency` config into the template so the page renders configured names server-side (no flash of 🪙 during HTMX load). |
| `GET /api/economy/leaderboard`, `/leaderboard-diamonds`, `/shop/items`, `/shop/purchase-history`, `/api/members` | **changed** — resolve config once per request and interpolate the configured emoji instead of the literals. |
| `GET /api/leveling/currency-rewards`, `/api/minigames/*`, `/api/trade/history`, `/api/ledger` | **changed** — attach resolved display info (`{"key","name","emoji"}`) so the templates stop hardcoding labels. |
| `GET /api/missions/list`, `/api/missions/completions` | **changed** — include resolved reward display so the missions table/log render the configured name without duplicating currency config in Missions. |

### 7c. Emoji normalisation — required by rule 4

The General Settings field is currently a bare text input labelled
*"Currency emoji ID (static emoji only)"* (`general.html:47`) and whatever the admin types is
stored **verbatim** and then interpolated directly into Discord message content. That means:

* a raw snowflake ID (`1532745018324815982`) renders as plain digits, not an emoji;
* `<:name:id>` and `<a:name:id>` work but animated emoji are explicitly disallowed by the label;
* unicode emoji work.

The repo **already has** the correct parser to reuse — it is simply not reachable from the
currency path:

```python
# dashboard/api/embedbuilder.py:253-262
_EMOJI_TOKEN_RE = re.compile(r"<(a?):(\w+):(\d+)>")
def _parse_emoji_input(raw):   # accepts raw ID | <:name:id> | <a:name:id>
```

Implementation note: hoist that parser into a shared helper (e.g. `utils/emoji.py`) so both
Embed Builder and Economy use **one** implementation, and add a `normalize_currency_emoji()`
that returns canonical renderable text:

| Input | Stored / rendered as |
|---|---|
| raw digits `1532745018324815982` | `<:emoji_1532745018324815982:1532745018324815982>` — or resolved to a real `name` if the bot can see it |
| `<:name:id>` | unchanged |
| `<a:name:id>` | unchanged (animated becomes allowed — the current "static only" restriction exists only because nothing normalises it) |
| unicode `🪙` / `قمر`-style text | unchanged |
| empty / whitespace | falls back to `DEFAULT_*_EMOJI` |

A raw-ID input cannot be turned into `<:name:id>` without knowing the name; the safe canonical
form for a raw ID is a `<:_:id>`-style token resolved through the bot's emoji caches
(`utils/app_emoji_cache.py`) or `discord.PartialEmoji`. The dashboard should therefore validate
on save and tell the admin plainly when an ID can't be resolved, rather than storing digits
that will render as text in Discord.

---

## 8. What should become the single source of truth

**`utils/currency.py` — one module, one read, one write.**

```python
# read (exists)                async def get_currency_config(guild_id) -> dict
# write (new)                  async def set_currency_config(guild_id, *, coin_name=None,
#                                                            coin_emoji=None,
#                                                            diamond_name=None,
#                                                            diamond_emoji=None) -> dict
# normalise (new)              def normalize_currency_emoji(raw) -> str
# lookup (exists, to be used)  def for_currency(config, currency_key) -> {"key","name","emoji"}
# display (new)                def currency_label(config, key) -> "🪙 Coins"
#                              def currency_amount(config, key, n) -> "🪙 1,250 Coins"
```

Rules to enforce (and to encode in the tests):

1. `utils/currency.py` is the **only** module that names those four DB columns. A test greps
   the tree for `currency_name|coin_emoji_id|diamond_name|diamond_emoji_id` outside
   `utils/currency.py` and `database.py` and fails on any hit.
2. Every user-visible currency string resolves through `for_currency()` / `currency_label()` /
   `currency_amount()`. No file re-declares a default name or emoji.
3. **Missions do not gain a currency table, column, cache, or constant.** Missions store
   `reward_type` keys (already true) and call the helper for display only (rule 10).
4. A blank/NULL/whitespace value always resolves to the `DEFAULT_*` constant (rules 2 and 5).
5. Changing a name or emoji in Economy is visible immediately everywhere with no restart,
   no cache invalidation step, and no edit to any mission definition (rules 1, 3, 9).

---

## 9. Conflicts with the existing Economy system

| # | Conflict | Detail | Resolution |
|---|---|---|---|
| C1 | **`currency_emoji_id` phantom column** | `dashboard/api/misc.py:76,85,96` and `dashboard/app.py:1433,1442,1453` INSERT it; fresh-DB schema has `coin_emoji_id` instead. Verified `OperationalError`. Because it's one INSERT, the **whole General form fails** — prefix, timezone and log channel too. | Remove the column from both writers (D1); add the legacy-value copy (D2). |
| C2 | **Emoji saved ≠ emoji rendered** | On *legacy* DBs the column exists, so the save "succeeds" — but `utils/currency.py` only reads `coin_emoji_id`, which the UI never writes. The icon the admin sets is silently ignored. | The new Economy writer writes `coin_emoji_id`; the legacy column becomes read-only history (D2). |
| C3 | **Diamond currency is unconfigurable** | `diamond_name` / `diamond_emoji_id` have no UI anywhere. | New Economy Currency tab exposes all four fields. |
| C4 | **Two duplicate general-settings writers** | `dashboard/app.py:1408` (`/config/general` POST) and `dashboard/api/misc.py:61` (`/api/settings/general`) implement the same upsert independently. They have already drifted from the schema — once, in the same way, twice. | De-duplicate: both delegate to one shared `save_general_settings()` (and the currency part removed entirely). |
| C5 | **`events.py` NameError** | `cogs/events.py:239` → hardcoded 🪙/💎 fallbacks sit inside code that can never run. The currency refactor almost certainly introduced the `interaction` reference while adding config lookups. | Pass `guild_id` into `_launch_event()` (or the already-resolved `cur`) and drop the fallback dicts. Must be fixed alongside the currency work — it is the same code path. |
| C6 | **Fallback defaults re-hardcode currency** | `cogs/missions.py:133,136`, `cogs/events.py:241,245`, `cogs/wallet.py:1035` each re-invent the default icon. If a guild sets a custom emoji and the config read ever fails, the UI silently reverts to 🪙/💎. | Since `get_currency_config()` already returns defaults on a missing row, the outer fallbacks are redundant. Remove them; let the helper be the only fallback authority. |
| C7 | **`bot_settings` daily range is global, currency config is per-guild** | Flagged in `WALLET_HANDOFF.md:200`. `daily_min`/`daily_max`/`daily_streak_bonus_per_day` have no UI and are shared by every server, while currency names are per-guild. | Out of scope for this task, but note it: the Currency tab makes the per-guild/global asymmetry more visible to owners. |
| C8 | **Exports are dead or unused** | `coin_name/coin_emoji/diamond_name/diamond_emoji` (never called), `format_coins()` (never called), `cogs/trade.py:11` unused import. | Either use them in the new display helpers or delete them — leaving them invites a third competing convention. |
| C9 | **A test currently locks in a hardcode** | `scripts/test_missions_v2.py:536-537` asserts `cog.DIAMOND_EMOJI == "<a:diamond:…>"`, and `:23` documents `"⤷ Reward claimed <a:diamond:…>"` as the expected render (stale — the code now emits `` `reward claimed` <dynamic> ✅ ``). | Update the test in the same pass as removing the constant, and add new assertions for dynamic resolution (custom name/emoji, defaults, per-guild isolation). |
| C10 | **`utils/currency.py` header documents the wrong rationale** | Its docstring (`:10-15`) argues currency must live in **General settings** "because owning it inside the economy module would force every other system to import an economy namespace". That reasoning is now superseded: `utils/currency.py` is a shared util, not an economy namespace, so Economy can own the *configuration* while every system imports the *util*. | Rewrite the docstring so the next session doesn't read the old rule as still binding. |

---

## 10. Proposed implementation plan (revised — Revision 2)

Ordered so that each phase is independently shippable, and the two 🔴 bugs are fixed first.
Phases marked **NEW** were added by the Revision 2 UI requirements (§13).

### Phase 0 — Pre-flight (no behaviour change)
* Add `scripts/test_currency_config.py` as the regression harness: assert defaults, custom
  values, per-guild isolation, blank⇒default, and that a currency rename changes rendered
  output in wallet / missions / shop / trade / economy / members.
* Add the **no-hardcode grep test** here, not at the end (rule 8): fails on any production
  `.py`/`.html` outside `utils/currency.py` containing a hardcoded currency name/emoji,
  `DIAMOND_EMOJI`, or a `✅`/`🪙`/`💎` literal in a Discord-facing string.
* Record the 3 baseline suites above as the regression gate.
* Add `scripts/test_ui_emoji.py` (**NEW**) — asserts every Discord-facing check renders
  `<a:check:1549593658867712090>`, that no dashboard HTML/JS emits a `<a:check:…>` token, and
  that the missions refresh button is `ButtonStyle.secondary`.

### Phase 1 — Fix the two 🔴 bugs (smallest possible diff)
1. Remove `currency_emoji_id` from `dashboard/api/misc.py:76,85,96` and
   `dashboard/app.py:1433,1442,1453` → General Settings saves again.
2. `database.py`: add the D2 legacy-copy migration + the D3 self-heal.
3. `cogs/events.py`: pass `guild_id` into `_launch_event()`; replace
   `interaction.guild.id if interaction.guild else 0` with the parameter; delete the fallback
   dicts and let `get_currency_config()` supply defaults.
4. Verify `/config/general` saves and a coin event actually posts.

### Phase 2 — Harden `utils/currency.py` as the single source of truth
* Add `set_currency_config()` (the only writer), `normalize_currency_emoji()`,
  `currency_label()`, `currency_amount()`.
* Hoist `_parse_emoji_input` out of `dashboard/api/embedbuilder.py` into a shared
  `utils/emoji.py`; reuse from both Embed Builder and Economy (one parser, no fork).
  **`normalize_currency_emoji()` must accept the animated form** — the current
  "static emoji only" restriction is dropped, since the repo's own locked emoji
  (`<a:diamond:…>`) is animated and rule 4 requires the supported emoji format set.
* Decide and act on C8: use or delete `coin_name/coin_emoji/diamond_name/diamond_emoji`
  and `format_coins`; drop the unused `cogs/trade.py:11` import.
* Rewrite the module docstring (C10).

### Phase 3 — Move the UI: General Settings → Economy
* Delete `dashboard/templates/config/general.html:39-53`.
* Add the Currency tab to `systems/economy.html` with 4 fields + preview + reset-to-default,
  wired to `POST /api/economy/currency`.
* Add `GET /api/economy/currency`; pass resolved config into the `/economy` page render.
* De-duplicate the two general-settings writers (C4).
* **Field behaviour to implement exactly:** both fields optional, independently saved,
  blank ⇒ default (§13.4). No "both required" validation.

### Phase 4 — Dynamic display everywhere
* Discord: `cogs/trade.py` buttons/modals/history, `cogs/wallet.py:1034-1035`,
  `cogs/missions.py` `_reward_display_sync` (add the configured **name**, drop fallbacks),
  `cogs/missions.py:439,467` admin echoes.
* Dashboard APIs: `core.py:276`, `economy_shop.py:61,97,177,393`, `leveling.py:192`,
  `trade.py`, `minigames` — resolve config once per request, return `{key,name,emoji}`.
* Dashboard templates: `economy.html`, `leveling.html`, `shop.html`, `ledger.html`,
  `missions.html`, `minigames.html`, `minigame_builder.html`, `trade.html`, `members.html`,
  `member_profile.html`, `base.html:474`.
* Missions rule 10 check: confirm no currency constant/table/cache was added to
  `utils/mission_engine.py` or `cogs/missions.py`.

### Phase 5 — UI emoji pass (**NEW**) — the check icon and the refresh button
Split by render target, because **one string cannot serve all three** (§13.2):
1. `utils/emoji.py`: add `CHECK_EMOJI = "<a:check:1549593658867712090>"` (Discord token) and
   `CHECK_EMOJI_CDN = "https://cdn.discordapp.com/emojis/1549593658867712090.gif"` (browser).
2. **Discord-facing `✅` → `CHECK_EMOJI`** — 16 production sites (§13.3 group A).
3. **Dashboard HTML/JS group B → `CHECK_EMOJI_CDN`** via a shared `checkIconHtml()` helper
   (`embed-composer.css` already ships the `.eb-inline-emoji` sizing pattern to reuse) — 13
   sites. **Must not** receive the raw `<a:…>` token.
4. **Console group C → leave unchanged** (`database.py:1730,1731,1758`, `main.py:331`).
5. Leave the **emoji-picker palette** (`manage/embedbuilder.html:421`) and the
   **`rules_button_text` defaults** alone, pending your decision (§13.5 — flagged, two
   scope questions).
6. `cogs/missions.py`: delete `CHECKMARK_EMOJI`; `REFRESH_EMOJI` moves to `utils/emoji.py`.
7. `cogs/missions.py:314`: `ButtonStyle.primary` → `ButtonStyle.secondary` (gray/neutral).
8. Capability check per §13.1: assert at startup that the check emoji is resolvable, and
   document the fallback if the bot loses access to it.

### Phase 6 — Verification
* Update `scripts/test_missions_v2.py` (C9 + the stale docstring at `:23`) and
  `test_wallet_phase2.py:77` (write through `set_currency_config()` instead of raw SQL).
* Re-run all suites + a full `init_db()` twice to prove migration idempotency.
* Re-run the Phase-0 no-hardcode grep test as the final gate.
* End-to-end manual pass, on the **exact scenario you gave**: set `coins → Moon` +
  custom emoji, `diamonds → Crystals` + custom emoji, then check `/wallet`, `/missions`,
  `/shop`, `/balance`, `/trade`, `/richest`, `/streak`, the Economy page, Shop page, Ledger
  page, Missions dashboard, Members list and member profile — all showing `Moon` / `Crystals`
  and their emojis, with no code edit and no restart. Then rename `Moon → قمر` **only in the
  Economy form** and confirm every one of those surfaces — including *already-completed*
  missions and existing ledger rows — now reads `قمر`, with zero edits to mission
  definitions or stored reward data (rules 1, 3, 9).

---

## 11. Verification pass — plan re-checked against the repository

Each plan item was re-validated against the actual source after drafting.

| Claim | Re-verified how | Result |
|---|---|---|
| `currency_emoji_id` does not exist on a fresh DB | Built a scratch DB via `database.init_db()`, read `PRAGMA table_info(guild_settings)`, then executed the exact INSERT statement from `misc.py` | ✅ reproduced — `OperationalError: table guild_settings has no column named currency_emoji_id` |
| General-settings save failure is total, not partial | Read the statement: one `INSERT ... ON CONFLICT DO UPDATE` covering prefix, timezone, language, log_channel_id, currency_name, currency_emoji_id, status rotation; `run_async()` calls `future.result()` which re-raises into Flask | ✅ whole row fails |
| `_launch_event` has an unresolved `interaction` | AST walk of `cogs/events.py`: params = `{self, channel, event_id, title, desc, reward_type, reward_value, reward_dur, max_winners, embed_data_str}`; `interaction` is not a param, local, or module global | ✅ confirmed `NameError`, swallowed at `:262` |
| No `Moon` anywhere | `grep -rin "moon"` over every file (excluding `node_modules`, binaries) + `git log --oneline --all` (2 commits) | ✅ zero hits |
| No display name is stored as a DB *value* | Inspected every currency-bearing column: `economy`, `transaction_ledger.currency`, `leveling_currency_rewards.currency`, `item_catalog.value_currency`, `purchase_history.currency_paid`, `missions_definitions.reward_type`, `events.reward_type`, `minigame_rewards.reward_type` | ✅ keys only — **no data migration needed for a rename** |
| Missions already grant dynamically | Traced `record_activities → give_reward → safe_credit`: `currency = "balance" if reward_type == "coins" else "diamonds"` | ✅ rules 6 & 7 hold at the logic layer |
| `DIAMOND_EMOJI` is dead | `grep -n "DIAMOND_EMOJI" cogs/missions.py` → declaration `:65`, comment `:70`, no use; only `CHECKMARK_EMOJI` is used (`:160`) | ✅ dead, but test-locked |
| `coin_name/coin_emoji/diamond_name/diamond_emoji` are dead | `grep -rn "\bfn("` for each across all `.py` → only the definitions | ✅ 4 dead helpers |
| `format_coins` is dead | same method | ✅ never called |
| `economy_safe` / `ledger` / `trade_engine` / `inventory` / `minigames` need no change | Read each; all key-based, all validate against `VALID_CURRENCIES` | ✅ correct as-is |
| The exchange-rate precedent supports Option A | `get_guild_exchange_rate()` reads `guild_settings.diamond_exchange_rate`, owned by the Economy page only | ✅ same table, Economy-owned |
| A reusable emoji parser already exists | `dashboard/api/embedbuilder.py:253-262` `_parse_emoji_input` handles raw ID + `<:name:id>` + `<a:name:id>` | ✅ hoist rather than rewrite |
| Baseline is green | Ran `test_missions_v2.py` (147 ✅), `test_wallet_phase2.py` (54 ✅), `test_wallet.py` (73 ✅) | ✅ no pre-existing failures to confuse a regression |
| Nav placement for the new tab | `base.html:221` Economy entry; `base.html:324` General Settings entry; `economy.html:9` existing tab loop | ✅ Currency tab slots into the existing loop |

### Corrections found during verification (already folded into the plan above)

1. **`cogs/trade.py:11` imports four currency helpers it never uses** — originally recorded as
   "trade is partially configured"; it is in fact configured for the *embed* (`build_embed`,
   dynamic) but hardcoded for the *buttons, modals and history* (§3b). Phase 4 covers both.
2. **`cogs/missions.py` `_reward_display_sync` omits the configured name**, printing only the
   emoji (`f"{rv} {emoji}"`). The requirement "UI labels … must follow the same configuration"
   makes this a real gap, not a cosmetic one — folded into Phase 4.
3. **`cogs/wallet.py:1034-1035` is not purely cosmetic.** The maps are overwritten in
   `open_receipts_panel` (`:1076-1080`), but `CurrencyTabButton.__init__` is also constructed
   on the hub → Receipts path, so a second copy of the defaults genuinely exists. Folded into
   Phase 4 rather than dismissed.
4. **D5 is unnecessary** — `utils/mission_engine.py` needs no schema work at all for this task.
   Kept in the table only to record that it was checked against rule 10.
5. **`scripts/test_missions_v2.py:23`** documents the *old* render (`⤷ Reward claimed <a:diamond:…>`)
   while the code renders `` ⤷ `reward claimed` <dynamic> ✅ ``. The docstring is stale
   independently of the assertion at `:536` — both need updating in Phase 5.

---

## 12. Revision 2 — what changed and why

The Revision 2 requirements added two UI mandates (animated check emoji, neutral refresh
button) on top of the currency architecture work. Three findings materially changed the plan:

| # | Finding | Plan impact |
|---|---|---|
| R1 | **The check emoji is not a find-and-replace.** `✅` renders in three structurally different targets (Discord, browser, console) that each need a different representation. §13.2 | Split into Phase 5, and a new `scripts/test_ui_emoji.py` gate in Phase 0 to stop a token leaking into HTML |
| R2 | **The check emoji must be resolvable by the bot.** `cogs/missions.py:68-75` already documents this failure mode for the sibling emoji. §13.1 | Phase 5 adds a startup capability check + soft fallback; **needs your answer on open question 1** |
| R3 | **`rules_button_text` adds 4 previously-uncounted `✅` sites** and is stored/admin-editable data. §13.6 | Excluded from the mechanical pass; flagged as open questions 2 |

The currency architecture conclusions from Revision 1 are **unchanged** — in particular, the
`custom value → default value if empty` behaviour you specified is already implemented
per-field in `utils/currency.py:57-64` (§13.4), so Phase 3 only has to avoid *breaking* it
with coupled form validation.

**Added to the plan:** Phase 0 gains the no-hardcode grep gate and the UI-emoji test;
Phase 2's `normalize_currency_emoji()` must accept animated emoji; Phase 5 is entirely new;
Phase 6 gains the `Moon → قمر` rename scenario as the definition-of-done.

---

## 13. UI requirements (Revision 2)

### 13.1 The animated check emoji — one blocking caveat first

Target: `<a:check:1549593658867712090>`.

`cogs/missions.py:68-75` already documents the failure mode for the sibling emoji, verbatim:

> *"Note: this is a GUILD emoji — if it's ever deleted from the server, the button silently
> falls back to no glyph (still fully functional)…"*

That applies with full force to the new check emoji. **A `<a:name:id>` token only renders in
Discord if the bot has access to that emoji** — either it lives in a guild the bot is in, or it
is one of the bot's own *application* emojis. If the ID belongs to a server the bot has left,
every one of the 16 sites in §13.3 group A will render the literal text
`<a:check:1549593658867712090>` to members. Because it is animated (`.gif`), a static-only
fallback path would also drop the animation.

Two independent safeguards, both cheap, both recommended:

1. **Resolve it once at startup** and log loudly if it fails — the repo already has the
   infrastructure: `utils/app_emoji_cache.py` imports any emoji as a **bot-owned application
   emoji** (usable in any guild, no `USE_EXTERNAL_EMOJIS` permission), and
   `dashboard/api/embedbuilder.py:329-388` already drives that import path. Pointing it at
   `1549593658867712090` makes the check emoji permanently available bot-wide instead of
   dependent on one guild's emoji staying alive.
2. **Fail soft at render time** — if resolution fails, fall back to plain `✅` rather than
   emitting a broken token. This is the one place a hardcoded `✅` is legitimate: it is a
   *degradation* fallback behind the configured value, not a competing source of truth.

**Please confirm:** is `1549593658867712090` an emoji from a server the bot is in, or should
Phase 5 import it as an application emoji first? This is the only item I cannot verify from
the repository alone — I have no Discord credentials in this environment to resolve it.

### 13.2 The check emoji is NOT a find-and-replace — three render targets

`✅` appears **53 times** across the repo in three structurally different contexts. A single
substitution would break the dashboard and pollute console output.

| Group | Renders in | Correct form | Count |
|---|---|---|---|
| **A. Discord** | messages, embeds, buttons | `<a:check:1549593658867712090>` | 16 prod sites |
| **B. Dashboard** | browser HTML/JS | `<img src="…/emojis/1549593658867712090.gif">` | 13 sites |
| **C. Console** | terminal (`print`) | leave `✅` alone | 4 sites |

An `<a:check:…>` token in an HTML template shows the admin literal angle-bracket text. And a
Discord token in `print()` output is terminal noise. Both mistakes are silent — neither
raises — which is exactly why this is worth stating explicitly.

### 13.3 Complete `✅` inventory, classified

**Group A — Discord-facing (16 sites → `CHECK_EMOJI`)**

```
cogs/backup.py:159,162        backup secondary-copy note + success confirmation
cogs/boost.py:261             boost colour option added
cogs/events.py:349            event list status "✅ Active"
cogs/leveling.py:637          leaderboard reset confirmation
cogs/minigames.py:545         minigame queued confirmation
cogs/missions.py:66           CHECKMARK_EMOJI constant        ← delete, replace with utils.emoji
cogs/missions.py:160          "⤷ `reward claimed` … ✅"        ← the mission reward line
cogs/missions.py:439          "/mission_create" confirmation
cogs/missions.py:467          "/mission_list" enabled status
cogs/mvp.py:349               forced-MVP confirmation
cogs/report.py:114            report sent confirmation
cogs/report.py:164            Resolve button emoji (success style)
cogs/scheduler.py:206         scheduled-message confirmation
cogs/shop.py:41               "(equipped)" inventory label
cogs/shop.py:386              purchase-success embed title
cogs/trade.py:62              trade offer "ready" mark
cogs/trade.py:317             "Ready" button emoji (success style)
cogs/trade.py:349             "✅ Trade complete" embed title
cogs/triggers.py:324          trigger status column
cogs/twitch.py:399            twitch config status
cogs/welcome.py:131           welcome-role granted confirmation
cogs/youtube.py:569           youtube config status
utils/minigame_engine.py:617,622,640   correct answer ×2 + answer-set confirmation
```

(23 line-level hits; the distinct *reward/success* sites you named are
`cogs/missions.py:160`, `cogs/trade.py:62,317,349`, `cogs/shop.py:41,386` — but the
requirement says "everywhere the relevant check/success indicator is used", so all of
group A is in scope.)

**Group B — Dashboard (13 sites → CDN image, NOT the token)**

```
dashboard/static/js/dashboard.js:60     toast success icon
config/boost.html:7                     save banner
config/general.html:7                   save banner
config/welcome.html:7                   save banner
config/botprofile.html:386              "Applied live"
general/member_profile.html:101,133     empty-state icons
general/member_profile.html:184         "Saved!"
manage/commands.html:946,1121           "Saved" / restrictions applied
manage/embedbuilder.html:1060           "Sent!"
systems/leveling.html:727               "Saved!"
systems/tagpartners.html:89             enabled column
```

Note `dashboard/static/js/dashboard.js:60` is the shared toast helper — one change covers
every toast in the dashboard.

**Group C — Console (4 sites → unchanged)**

```
database.py:1730,1731,1758     init / bypass / owner-grant prints
main.py:331                    owner-backfill print
```

**Two sites that must stay `✅` deliberately** — see §13.5.

### 13.4 Default behaviour — `custom value → default value if empty`

This is already how `utils/currency.py` behaves (`:57-64`), and it already does the right
thing per-field:

```python
coin_name    = (row[0] or "").strip() or DEFAULT_COIN_NAME
coin_emoji   = (row[1] or "").strip() or DEFAULT_COIN_EMOJI
diamond_name = (row[2] or "").strip() or DEFAULT_DIAMOND_NAME
diamond_emoji= (row[3] or "").strip() or DEFAULT_DIAMOND_EMOJI
```

Each of the four fields falls back **independently**, so name-only, emoji-only, or
neither-config is all valid — exactly the "do not require the user to configure both fields"
requirement. Whitespace-only input also falls back (`.strip()` before the `or`). This is
verified behaviour, not a claim: `test_wallet_phase2.py` exercises the custom path, and §11
confirms the default path for unknown guilds.

**What is still missing for this requirement:** the new Economy form must not introduce
client-side validation that blocks a partial save (e.g. a "both fields required" guard or a
non-empty `required` attribute), and `set_currency_config()` must treat `""` and `None`
identically as "revert to default" rather than storing an empty string over a previously
customised value. The stored column should hold `NULL`/`''` for "unset" and the resolver
supplies the default — **the default must never be written into the row**, or a later change
to `DEFAULT_*` would fail to propagate to guilds that never configured anything.

### 13.5 Refresh button → gray/neutral

There is exactly **one** Discord refresh button in the bot:

```python
# cogs/missions.py:314-315
@discord.ui.button(label="ʀᴇꜰʀᴇꜱʜ", emoji=REFRESH_EMOJI,
                   style=discord.ButtonStyle.primary)      # ← blurple = the "colored appearance"
```

`ButtonStyle.primary` is blurple; the neutral/gray style is `ButtonStyle.secondary`. Change is
one token. `REFRESH_EMOJI` itself (`<:imagePhotoroom17:1549206183498481714>`) is a
*content* decision you did not ask to change, so it stays — only the button's colour changes.

For completeness, the other `🔄`-using Discord surface is `cogs/trade.py:311`
("Clear My Offer"), which is **already** `ButtonStyle.secondary` and is a clear action, not a
refresh — no change needed. Dashboard `🔄 Refresh` buttons (`ledger.html:18`,
`trade.html:11`, `mvp.html:29`) are already `btn-secondary` (gray) in
`main.css:450`, so the dashboard already matches the requested appearance.

### 13.6 Two scope questions I need you to decide

Both are places where a mechanical replacement would change **stored data** or remove a
legitimate feature, so I am not guessing:

1. **`rules_button_text`** — default `'✅ I Accept'` in `database.py:587`, the welcome form
   (`welcome.html:91-92`), `dashboard/api/misc.py:148` and `dashboard/app.py:1518`
   (3 more `✅` sites, none counted above). This is an **admin-editable stored string**, not a
   hardcoded icon: changing the literal only affects *new* guilds (existing rows keep their
   stored value), and any guild that customised the text keeps it. Options:
   (a) leave it entirely; (b) change the default going forward + add a migration; (c) change
   the button *emoji* but leave the label text. **My recommendation: (a)** — it is a rules
   button label, unrelated to currency or to the reward/check semantics you described, and
   touching it risks rewriting an admin's custom copy.
2. **Emoji-picker palette** (`manage/embedbuilder.html:421`) — `✅` here is one of ~40
   unicode symbols an admin can click to insert into an embed. It is a *choice offered to the
   admin*, not the bot's own check indicator. **My recommendation: leave it** — replacing it
   would remove an admin's ability to insert a plain unicode check.

### 13.7 Emoji-format requirement (rule 4) — restated with the check emoji in mind

The Economy emoji fields must accept all four forms and normalise them, so the *same* input
works for currency icons as for the check emoji:

| Admin enters | Stored / rendered |
|---|---|
| unicode `🪙`, `💎`, `🌙` | unchanged |
| `<:name:id>` | unchanged |
| `<a:name:id>` (animated) | unchanged — **now allowed** (see Phase 2) |
| raw ID `1549593658867712090` | normalised to a renderable token, or a clear error on save |

Raw-ID normalisation cannot invent a `name`, so the canonical resolution is through the bot's
emoji caches (`utils/app_emoji_cache.py`, `discord.PartialEmoji`). If an ID cannot be
resolved, the Economy form must say so plainly at save time — the current General Settings
field silently stores bare digits and renders them as text in Discord, which is the bug that
makes this requirement worth spelling out.

---

## 14. Final repository-wide search (Revision 2 verification)

Every string you asked for was searched across all `.py` / `.js` / `.html` / `.json` / `.md`,
excluding `node_modules` and `.git`. Production-only counts exclude `scripts/` and `tools/`.

| Search term | Result |
|---|---|
| `✅` | **53 total hits.** 23 Discord-facing (group A), 13 dashboard (group B), 4 console (group C), 2 deliberate (picker palette, `rules_button_text` ×2), 2 in test scripts, 19 in `CURRENCY_AUDIT.md` itself, 18 in `ALIAS_INVESTIGATION.md`, 1 in `MINIGAMES_V2_PLAN.md`. Full classification §13.3. |
| Existing check icons | The only additional check-ish glyphs are `❌` (26 sites, the error/fail counterpart — **out of scope**, you asked for the check/success indicator), `⛔` (`tagpartners.html:89`), `🏁` (`events.py:78,140`, race-finish), `⚫` (`events.py:349`, `twitch.py:118,398`, `youtube.py:232`, `creator_notify_engine.py:806`, "offline/ended"). None is a success indicator. |
| Refresh icons/buttons | **1 Discord refresh button** (`cogs/missions.py:314`, currently blurple). `🔄` elsewhere is either the non-refresh "Clear My Offer" (`trade.py:311`, already neutral) or dashboard buttons already `btn-secondary`. Two `🔄` are section headings, not buttons (`general.html:56`, `leveling.html:160`). §13.5. |
| `Coins` | §4 — 4 hardcoded production defaults (`wallet.py:1034`, `formatters.py:155`, `misc.py:95`, `app.py:1452`) + 2 DB column defaults + ~15 template labels. |
| `Diamonds` | §4 — same shape (`wallet.py:1034` + DB defaults + templates). |
| `Moon` | **0 hits in code.** Only in this audit document (as the requirement's example). Not in the schema, not in git history. |
| `قمر` | **0 hits in code.** Only in this audit document. Confirms the requirement is a *forward* capability, not a rename. |
| `crystal` / `Crystals` | **0 hits.** Same conclusion. |
| Hardcoded currency emojis | §4 — 14 production `🪙`/`💎` literals across `missions.py`, `events.py`, `trade.py`, `wallet.py`, `economy_shop.py`, `core.py`. |
| `DIAMOND_EMOJI` | `cogs/missions.py:65` (dead constant), `:70` (comment referencing it), and `scripts/test_missions_v2.py:537` (a test that **locks it in**). Nothing else. |
| Currency display helpers | `utils/currency.py` — `get_currency_config` (18 call sites), `for_currency` (6). Plus 4 **dead** exports (`coin_name`, `coin_emoji`, `diamond_name`, `diamond_emoji`) and `format_coins` (never called). §3a, §4. |
| Mission reward display | `cogs/missions.py:128-179` (`_reward_display_sync` / `_mission_block`), `:442`, `:473`, and `dashboard/templates/systems/missions.html:57-58,231,346`. §5. |
| Direct currency formatting | ~30 `f"{amount:,} {name}"`-style sites, overwhelmingly in `cogs/economy.py` (16) and `cogs/events.py` (5) — all already pulling `cc`/`cd` from config, but all hand-rolling the label instead of going through one formatter. This is the case for `currency_amount()` in Phase 2. |

### 14.1 Mission-reward key stability — re-verified specifically

Your requirement: mission definitions must store the **stable key**, never the display name.

| Check | Method | Result |
|---|---|---|
| `missions_definitions` has no name/emoji column | Read `utils/mission_engine.py:86-101` | ✅ only `reward_type` (key) + `reward_value` (amount/ID/name) |
| `VALID_REWARD_TYPES` is key-only | `utils/mission_engine.py:64` | ✅ `("coins", "diamonds", "xp", "role", "temp_role", "item")` — no display names |
| Validation rejects non-numeric amounts for currency rewards | `_validate_reward_value()` `:187-201` | ✅ `int(reward_value)` required |
| Grant path resolves key → column, never a name | `utils/reward_engine.py`: `currency = "balance" if reward_type == "coins" else "diamonds"` | ✅ name-independent |
| Display path resolves key → live Economy config | `cogs/missions.py:204-210` `get_currency_config(guild.id)` called per render | ✅ not cached in the row, not cached in the module |
| **Renaming invalidates existing missions?** | Follow the data: the stored value is `'coins'`; the rendered string is computed at **render time** from `guild_settings` | ✅ **No — impossible.** A completed mission's stored row is untouched by a rename; the next render reads the new config. Rule 9 holds by construction. |
| Same for Diamonds | identical path, `reward_type='diamonds'` → `economy.diamonds` | ✅ |
| Same for the ledger | `transaction_ledger.currency` stores `'balance'`/`'diamonds'`; Wallet receipts resolve labels at render time (`cogs/wallet.py:950-990`) | ✅ history re-labels automatically |

**Conclusion:** the key-stability requirement is already satisfied at the data layer. The work
is to stop the *display* layer from re-hardcoding what the data layer got right.

### 14.2 Corrections found during this second pass

1. **The check emoji cannot be a single find-and-replace** (§13.2). This is the most important
   finding in Revision 2 — stated up front because it changes Phase 5's shape.
2. **`scripts/test_missions_v2.py:23` documents a render that no longer exists**
   (`⤷ Reward claimed <a:diamond:…>`) — the code emits `` ⤷ `reward claimed` <dynamic> ✅ ``.
   Stale independently of the assertion at `:537`. Folded into Phase 6.
3. **`cogs/missions.py:160` is the single most important check site** — it is literally the
   mission-reward claimed line, i.e. the intersection of both requirements.
4. **`utils/minigame_engine.py:640`** ("✅ Answer set") is Discord-facing and must be in group
   A, even though minigames otherwise need no currency changes.
5. **`cogs/trade.py:317` and `cogs/report.py:164` carry `✅` as a *button* emoji**, which
   discord.py parses via `PartialEmoji.from_str`. A raw string constant works there, but the
   emoji must be bot-accessible or the button renders the token as its label — reinforcing
   §13.1.
6. **`dashboard/static/js/dashboard.js:60`** is the shared toast icon: one line covers every
   dashboard toast, the highest-leverage single change in group B.
7. **`rules_button_text` adds 4 more `✅` sites** (3 prod + 1 default) that were not in my
   first-pass inventory — surfaced only by this final search, hence §13.6 question 1.

---

## 15. Confirmations against your requirements

| Your requirement | Status in this plan |
|---|---|
| Animated check `<a:check:1549593658867712090>` everywhere the check/success indicator is used | ✅ §13.2/§13.3 — 16 Discord sites; **split from 13 dashboard + 4 console sites** because one string cannot serve all three; §13.1 flags the access dependency |
| Refresh button gray/neutral | ✅ §13.5 — one token, `cogs/missions.py:314`; dashboard already neutral |
| Currency UI moves General Settings → Economy | ✅ §6 + Phase 3 — delete the card, add a Currency tab to `/economy` |
| Keep it simple — no overcomplicated management system | ✅ 4 fields, one tab, one endpoint pair, no new table, no per-currency config objects |
| Primary: custom name + emoji ID. Diamond: custom name + emoji ID | ✅ exactly 4 columns, exposed as exactly 4 inputs |
| `custom value → default value if empty`, per field, both optional | ✅ §13.4 — already the resolver's behaviour; Phase 3 must not add coupled validation; defaults never written into rows |
| Economy config is the single source of truth project-wide | ✅ §8 — `utils/currency.py` is the only reader/writer; Phase 0 grep test enforces it mechanically |
| No hardcoded `Coins`/`Diamonds`/`Moon`/currency emojis in user-facing output | ✅ §4 inventory + Phase 4 + the Phase 0 gate |
| Internal keys stay stable (`coins`, `diamonds`) | ✅ §1 — no schema change, no data migration, `balance`/`diamonds` untouched |
| `coins → Moon`, `diamonds → Crystals` shows everywhere | ✅ Phase 6 end-to-end pass uses your exact scenario |
| Rename `Moon → قمر` with no mission/data edits | ✅ §14.1 — verified **impossible to break**, by construction: keys in storage, labels resolved at render time |
| Mission definitions store the key, not the name | ✅ §14.1 — already true; `VALID_REWARD_TYPES` is key-only |
| Reward engine resolves key → Economy config → name + emoji | ✅ already true for the balance mutation; Phase 4 completes it for *display* |
| Same for Diamonds | ✅ identical path, verified |
| Fix the architecture, don't patch screens | ✅ one resolver + one writer + one formatter + one emoji module, enforced by a repo-wide automated check rather than per-screen edits |

### Remaining open questions before implementation

1. **§13.1** — is emoji `1549593658867712090` already available to the bot, or should Phase 5
   import it as a bot-owned application emoji first? (I cannot resolve this without Discord
   credentials.)
2. **§13.6** — `rules_button_text` (`✅ I Accept`): leave as-is (my recommendation), or change?
3. **§13.6** — the emoji-picker palette's `✅`: leave as-is (my recommendation), or change?
4. **§6 storage** — confirm Option A (keep the four columns in `guild_settings`, Economy owns
   the writer) rather than a new `economy_config` table.

Everything else in the plan is verified against the repository and ready to build.
**Still no implementation started.**

---

## 16. Summary of the ten requested answers

*(Originally §12 in Revision 1; renumbered and updated for the Revision 2 UI requirements.)*

1. **Architecture** — two layers: internal storage keys (`balance`/`diamonds`, used by every
   table and every mutation) and display config (4 `guild_settings` columns) read through
   `utils/currency.py`. The separation is correct; the display layer is mis-owned and
   partially bypassed.
2. **Where `Moon`/Diamond config lives** — no `Moon` exists. Primary name/emoji and diamond
   name/emoji live in `guild_settings` (`currency_name`, `coin_emoji_id`, `diamond_name`,
   `diamond_emoji_id`); only 2 of 4 are reachable from any UI, and one of the two wired inputs
   writes a column that no longer exists.
3. **Dependents** — 18 `get_currency_config()` call sites across 6 cogs, plus 12 dashboard
   API/partial render sites and 12 templates that hardcode currency text or glyphs.
4. **Hardcodes** — 14 production 🪙/💎 literals, 4 hardcoded `"Coins"` defaults, the
   mission-module `DIAMOND_EMOJI` animated-emoji constant, and 5 dead helpers.
5. **Missions rewards** — keys only in storage (`coins`/`diamonds`/…), granted through the
   shared `give_reward()` → `safe_credit()` path with **no** hardcoded currency identity;
   display resolves config but drops the configured *name*, keeps 🪙/💎 fallbacks, and prints
   raw keys on admin surfaces.
6. **Move General → Economy** — delete the 2-field Currency card and both writers' currency
   columns; add a 4-field Currency tab to `/economy`; keep storage in `guild_settings`
   (same precedent as `diamond_exchange_rate`).
7. **DB/API** — fix the phantom-column writers, add the legacy-emoji copy and a `currency_name`
   self-heal, add `GET`/`POST /api/economy/currency`, strip currency from
   `/api/settings/general` and `/config/general`, and teach the dashboard partials to resolve
   config. **No data migration is required to rename a currency.**
8. **Single source of truth** — `utils/currency.py`: the only reader *and* writer of those four
   columns, the only place defaults live, with `set_currency_config()`,
   `normalize_currency_emoji()`, `currency_label()` and `currency_amount()`.
9. **Conflicts** — 10 identified (C1–C10), including two 🔴 runtime failures
   (`currency_emoji_id` 500, `events.py` NameError), the missing diamond configuration,
   duplicated general-settings writers, redundant fallback hardcodes, dead exports, a
   test that locks in a hardcode, and a docstring that documents the now-superseded ownership
   rule.
10. **Plan** — 7 phases (Phase 0 harness + grep gate → Phase 1 bug fixes → Phase 2 helper
    hardening → Phase 3 UI move General→Economy → Phase 4 dynamic display everywhere →
    Phase 5 UI emoji pass (check icon + neutral refresh) → Phase 6 verification), each
    independently shippable, verified against the repository in §11 and §14.
11. **UI (Revision 2)** — the animated check `<a:check:1549593658867712090>` replaces `✅` at
    16 Discord sites, but **not** at 13 dashboard sites (those need the CDN `.gif`) or 4
    console sites; the Missions refresh button flips from `primary` to `secondary`. §13.
12. **Mission key stability** — re-verified end-to-end: renaming a currency **cannot**
    invalidate an existing mission or stored reward, because storage holds keys and labels are
    resolved at render time. §14.1.

**Nothing has been implemented. Awaiting your go-ahead — and answers to the 4 open questions
in §15 — before Phase 1.**
