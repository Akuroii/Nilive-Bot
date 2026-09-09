# WALLET — implementation handoff

Branch: `arena/01a08804-nilive-bot` · Commit: `0b018ac`

Everything below is written so you can inspect, correct and improve it
against the real architecture. **The references were not treated as the
final design** — where I diverged from them, the reasoning is stated so
you can overrule it.

---

## 1. Exact files changed

### New files
| File | Purpose |
|---|---|
| `cogs/wallet.py` | `/wallet` hub + canonical `/streak`. All panels, views, embeds. |
| `utils/title_engine.py` | Title equip slot (equip / unequip / read / stale cleanup). |
| `utils/potion_engine.py` | Consumable "Use" path on top of the existing XP-boost effect. |
| `scripts/test_wallet.py` | 73-check verification suite (see §8). |
| `WALLET_HANDOFF.md` | This document. |

### Modified files
| File | Change |
|---|---|
| `utils/daily_engine.py` | Added the shared claim engine: `perform_streak_claim()`, `get_streak_state()`, `get_streak_preview()`, `get_daily_range()`, `format_remaining()`. Existing `claim_daily_streak()`/`get_streak_bonus()` untouched. |
| `cogs/economy.py` | `/daily` reduced from ~90 inline lines to a thin alias. Removed the now-unused `random` import. Nothing else touched. |
| `cogs/shop.py` | Added `potion` + `title` purchase branches and their validation; potion/title success-embed wording. Existing branches untouched. |
| `utils/ledger.py` | Added `count_user_ledger()` + `get_user_ledger_page()`. Existing functions untouched. |
| `utils/rank_card_data.py` | Added `equipped_title` to the returned dict (read-only). |
| `database.py` | Added `equipped_titles` table (see §2). |
| `dashboard/api/economy_shop.py` | Potion validation + **pre-existing bug fix** (see §7). |
| `dashboard/templates/systems/shop.html` | `potion` / `title` options and their field toggling/validation. |
| `dashboard/app.py` | Registered `wallet` + `streak` in the Economy command category and metadata. |
| `main.py` | Loads `cogs.wallet`. |
| `.gitignore` | Added `.venv/` (I created one to run the test suites). |

---

## 2. Database changes

**One new table. No migration of existing data. Nothing dropped, renamed or rewritten.**

```sql
CREATE TABLE IF NOT EXISTS equipped_titles (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    item_name   TEXT NOT NULL,
    equipped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
)
```

**Why it was genuinely required, and why not a column on `equipped_roles`:**
`equipped_roles`' primary key is `(guild_id, user_id)` with a `NOT NULL
role_id`. That PK is exactly what enforces the "one equipped role"
invariant. A title sharing that row would have needed either a fake
`role_id` or a relaxed `NOT NULL` — both weaken the invariant the table
exists to protect, and both break the locked decision that the Title
slot is **independent** from the Role slot. A separate table with the
same shape gives one title per member with zero impact on the role slot.

**No table was needed for anything else:**
- **Receipts** → `transaction_ledger` already records every coin/diamond
  inflow and outflow. I added a paginated reader, not a table.
- **Potions** → effect parameters ride in `inventory_items.metadata`, the
  existing JSON column role items already use for `{"role_id": ...}`.
- **Potion/title shop items** → reuse `xp_boost_multiplier`,
  `duration_hours`, `icon_url`, `rarity`. No new columns.

Verified non-destructive: I built a pre-Wallet legacy DB with real rows
in `economy`, `daily_claims`, `inventory_items`, `equipped_roles`,
`shop_items`, `transaction_ledger`, ran the new `init_db()` twice over
it, and confirmed **every original row came back byte-for-byte identical**.

---

## 3. What was reused (deliberately, instead of rebuilt)

| Reused | Instead of |
|---|---|
| `transaction_ledger` + `utils/ledger.py` | A new receipts/history table |
| `utils.daily_engine.claim_daily_streak()` atomic guard | A new cooldown mechanism |
| `daily_claims` rows and streak data | Any reset or migration |
| `utils.economy_safe.safe_credit` (auto-ledgers) | Manual receipt writes |
| `utils.xp_calculator.grant_xp_boost` + `leveling_active_boosts` | A new potion effects framework |
| `utils.inventory` (`give_item`/`remove_item`/`get_inventory`) | New inventory plumbing |
| `utils.equip_engine.equip_role()` | A second role-equip path |
| `utils.item_catalog` (`icon_url`, rarity, `item_sort_key`) | Inventing item icons |
| `utils.reward_engine.give_reward(type="item")` | A bespoke delivery path |
| `utils.prestige.get_prestige_earn_multiplier` | Any new prestige logic |
| Shop `xp_boost_multiplier` / `duration_hours` columns | New potion columns |

---

## 4. What was newly created

- **`/wallet`** — hub with balances, item count, live streak state; buttons
  for Streak / Inventory / Receipts / Vote (Vote is **disabled and inert** —
  not implemented, per scope).
- **`/streak`** — canonical command. `/daily` kept as a thin alias.
- **Shared claim engine** — `perform_streak_claim()`. `/streak`, `/daily`
  and the Wallet button are three entry points into **one** implementation
  and **one** claim guard.
- **Inventory manager** — three tabs (Items / Potions / Titles), per-stack
  quantities, rarity labels, item detail view with state-derived actions.
- **Title system** — minimal, clean, extensible.
- **Potion "Use"** — consumable path with a refund on failed effect grant.
- **Receipts** — Coins/Diamonds tabs, 8 per page, Back/Forward.

---

## 5. Assumptions I made (please check these)

1. **Roles belong in the "Items" tab.** The three tabs are Items / Potions
   / Titles as specified, so `role`/`temp_role` items had nowhere else to
   go. From a member's perspective they're "things I own and can wear".
   If you want a fourth Roles tab, it's a small change.
2. **`icon_url` can't render inline in an embed list.** The Shop stores item
   art as a *URL*, which Discord can't show in a text line. So: if
   `icon_url` holds a custom-emoji mention (`<:name:id>`) it's used as the
   list emoji; otherwise the category emoji is used, and the URL renders as
   the **thumbnail on the item detail view**. I did not invent unrelated
   icons. **You may want an `emoji` field on shop items** — see §10.
3. **Roles have no Unequip.** `equip_engine.equip_role()` is swap-only —
   there is no "wear nothing" path in it. So an equipped role shows a
   disabled "Equipped" state rather than an Unequip button that the engine
   can't honour. **Titles do have Unequip** (their engine supports it).
4. **Potion = deferred XP boost.** The only timed-effect infrastructure in
   the project is `leveling_active_boosts`. I did not invent a second one.
5. **Ledger `source='daily'` was kept** (not renamed to `streak`) so existing
   ledger rows and any dashboard filtering keep matching.
6. **Receipts show 8/page** (within your 5–10 range).
7. **Streak resets at 00:00 UTC** — that is the existing engine's behaviour,
   not a new choice.

---

## 6. Design improvements over the references

1. **One ephemeral message, edited in place.** Every panel re-renders the
   same message. Ephemeral messages can't be dismissed by the user, so
   spawning a new one per click leaves a trail of stale, still-clickable
   panels. This also makes "Back" meaningful.
2. **The hub shows counts, not lists.** Members open a wallet to ask "how
   much do I have / can I claim yet". The bag is one click away instead of
   flooding the first screen — the main departure from the competitor's
   dump-everything-in-one-list approach.
3. **Actions are derived from type + state.** A potion never offers Equip;
   an equipped title offers only Unequip; an unusable potion's Use button is
   disabled. **No action the member sees is one the backend would reject.**
4. **The streak button reflects state before it's clicked** — green when
   claimable, muted when already claimed.
5. **Honest streak reporting.** `get_streak_state()` returns `0` when the
   chain is already broken rather than the stale stored count — showing
   "Day 7" right before a claim silently restarts at Day 1 is a lie. The
   stored count is still preserved.
6. **Preview before claiming.** The Streak panel shows the reward range and
   the bonus *the next claim would actually pay* (streak + 1) without
   consuming anything.
7. **Active tab = disabled primary button.** Reads as "you are here" with no
   extra label and can't re-render the panel it's already showing.
8. **Ownership checked twice.** Ephemeral controls visibility only; every
   component also re-checks `interaction.user.id` via one shared
   `interaction_check`, so a new button can't forget the guard.
9. **Views are ephemeral-lifetime with `on_timeout` disabling controls** —
   wallet panels carry per-user navigation state, which must *not* survive a
   restart (unlike the stateless persistent `shop_buy_*` buttons).
10. **Pagination is correct at boundaries.** Ordering adds `id DESC` as a
    tiebreaker because `created_at` is second-resolution — a convert writes
    two rows in the same second, and without the tiebreaker rows can
    duplicate or vanish across a page boundary.
11. **Unknown ledger sources are shown, not hidden** — an unlabelled
    transaction is still money that moved.
12. **Potion refund on failure.** If the effect grant fails after the stack
    is decremented, the copy is returned. The member is never charged for
    nothing.

---

## 7. Economy issues discovered

### 7a. Real bug, fixed (it blocked potions)
`dashboard/api/economy_shop.py`'s `add_shop_item()` INSERT **never included
`xp_boost_multiplier`**, although the field is in the form, in the JS
payload, and read back by the purchase path. Every XP Boost item created
from the dashboard therefore saved with `NULL` and was **permanently
unbuyable** — "This XP boost item isn't configured correctly". Potions reuse
that column, so this had to be corrected rather than worked around.
**→ Any existing XP Boost shop items are still broken and need re-saving.**

### 7b. Flagged, deliberately NOT changed (your review is pending)
- The reward formula was **moved verbatim**, not redesigned:
  `random(daily_min, daily_max) + min(streak,7) × 20`, with the Prestige
  multiplier applied to the **combined** total. Nothing is hard-coded to
  this prompt; the displayed Bonus reads from the same settings, so when you
  change the model the UI follows automatically.
- `daily_min`/`daily_max`/`daily_streak_bonus_per_day` live in `bot_settings`
  with **no dashboard UI** — they're only editable by direct DB write.
- `bot_settings` is **global, not per-guild**, while `currency_name` and the
  exchange rate are per-guild. Every server shares one daily range.
- The streak bonus is **flat** (`days × 20`), so the day-7 bonus (140) can
  exceed the base reward (100–300) in a way that may not be intended.
- `get_streak_bonus()` doesn't scale with `daily_min`/`daily_max`, so raising
  the daily range makes the streak bonus proportionally irrelevant.

---

## 8. Tests performed

`scripts/test_wallet.py` — **73 checks, all passing**. Runs the real engines
against a scratch DB created by the project's own `init_db()`, so schema
drift is impossible.

```
.venv/bin/python scripts/test_wallet.py
```

Covers: migration idempotency · claim crediting + ledger row · same-day
rejection · **shared guard across all three entry points** · streak
continuation/reset · bonus cap vs uncapped count · honest broken-chain
reporting · preview consuming nothing · title equip/unequip · **title/role
slot independence** · ownership enforcement · stale-title cleanup · potion
consumption + effect · last-copy handling · misconfigured potion not
consumed · **refund on failed grant** · pagination integrity · currency and
per-user isolation · tab routing · quantity aggregation · concurrent claims.

**Also verified separately:**
- All cogs load into a real `discord.py` command tree; `wallet`, `streak`,
  `daily`, `inventory`, `balance` all register.
- Every view instantiates within Discord's limits (≤5 components/row,
  ≤25 select options) including with 30 items and with empty tabs.
- Every embed builds under the 4096-char description limit.
- End-to-end potion/title purchase → inventory → use/equip, confirming
  **titles create zero Discord role rows**.
- Shop API now persists `xp_boost_multiplier`, and the purchase path reads
  it back correctly.
- Legacy-DB migration preserves all rows byte-for-byte.
- **Existing suites still pass: prestige (52), minigame engines (96),
  minigame store (87), minigame migration (32) — 267 checks, 0 failures.**

### Edge cases tested
Double-click / concurrent claim (only one wins) · claiming via `/daily` then
`/streak` then the button · claim losing a race mid-panel (button retires
with a precise timer) · broken vs live streak chain · streak past day 7 ·
using the last potion (returns to tab) · using an empty stack · potion with
missing effect params · effect grant failure · title equipped then removed
from inventory · unequipping an already-unequipped title · equipping a
non-title as a title · item deleted while its detail view is open (falls
back to the tab) · empty inventory tabs · 30+ items (select truncates at 25) ·
page 0 / last page / switching currency mid-pagination · empty receipts ·
reversed ledger entries · unknown ledger source · another member clicking
your wallet.

---

## 9. Not implemented (per scope)

Vote (button reserved and **disabled**) · coupons · dashboard redesign ·
rank card redesign · prestige changes · economy rebalancing · member sell
system · arbitrary refactors.

---

## 10. What Akuroi should specifically review

1. **Re-save any existing XP Boost shop items** — they were saved with a
   NULL multiplier by the bug in §7a and are still unbuyable.
2. **Restart the bot** so `init_db()` creates `equipped_titles`, and
   **re-sync commands** so `/wallet` and `/streak` appear.
3. **Decide the economy model** (§7b) — especially whether `bot_settings`
   should become per-guild, and whether the streak bonus should scale with
   the daily range. The UI already reads live settings, so no Wallet code
   should need to change.
4. **Item emoji**: consider adding a dedicated `emoji` field to shop items.
   `icon_url` is a URL and can't render inline; I use it as the detail-view
   thumbnail and support a custom-emoji mention in that field as an interim.
   The project already has `utils/app_emoji_cache.py` (application emojis,
   usable anywhere) — that's the clean long-term path.
5. **Roles in the Items tab** — confirm, or ask for a fourth tab.
6. **Role Unequip** — currently unsupported by `equip_engine` (swap-only).
   If you want true unequip, that belongs in `equip_engine`, not the Wallet.
7. **Potion = XP boost only.** `metadata` carries an explicit `effect`
   discriminator so a second kind can be added with no migration; unknown
   kinds are rejected rather than silently consuming the item.
8. **Should `/inventory` (in `cogs/shop.py`) be retired?** It still exists
   and overlaps the Wallet's inventory. I left it alone deliberately — that
   removal is a product call, not a refactor I should make unilaterally.
9. **`equipped_title` is exposed via `utils/rank_card_data.py`** ready for
   the Rank Card to consume. Nothing renders it yet.
