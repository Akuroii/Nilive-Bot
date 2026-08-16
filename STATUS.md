# NILIVE BOT — SHIFT CHECKPOINT — Rank Card foundation (pass 1: schema + backend)

## Done this pass

Schema + backend logic for the item catalog / rarity / equip system,
per the design locked with Dark over this session. NO visual card work
this pass — that's next session, per Dark's own ordering.

**Files:**
- `database.py` — 3 migrations: `shop_items.icon_url` / `.rarity`
  (nullable/defaulted, existing items unaffected), new `item_catalog`
  table, new `equipped_roles` table. Verified idempotent (ran
  `init_db()` twice against a scratch DB, no errors).
- `utils/item_catalog.py` — NEW. `RARITY_ORDER`
  (common→rare→epic→legendary→mythical→secret), `RARITY_COLORS`
  (Dark's exact locked gradient hex values, ready for the card
  renderer to import directly), `item_sort_key()` (rarity first,
  value tiebreaker), `get_catalog_entry()` / `upsert_catalog_entry()`.
- `utils/equip_engine.py` — NEW. `equip_role()` — the single place
  that does the actual Discord role swap (remove old equipped role if
  different, add new, update `equipped_roles`). `get_equipped()`,
  `cleanup_expired_role_item()` (wired into temp_role_cleanup).
- `utils/reward_engine.py` — role/temp_role branch rewritten: writes
  an `inventory_items` row (quantity fixed at 1) THEN calls
  `equip_role()` instead of a raw `add_roles()`. This one change
  covers shop, events, minigames, missions, tag missions, and tag
  partners automatically — none of those callers needed touching
  except cogs/shop.py (see below).
- `utils/inventory.py` — `set_quantity()` gained an optional
  `metadata` param (role_id + expires_at for role items), backward
  compatible.
- `utils/rank_card_data.py` — NEW. `get_rank_card_data()` — aggregates
  everything the future card renderer needs (xp/level/prestige,
  balances, message/voice totals, minigame wins, equipped role,
  rarity-sorted item grid) into one call. Pure data, zero image work.
  Built now so next session is visual-only.
- `cogs/shop.py` — `process_purchase()` now passes `item_name=name`
  to `give_reward()` so the inventory row shows the shop's configured
  name ("Flame") instead of the raw Discord role name.
  `temp_role_cleanup()` now calls `cleanup_expired_role_item()` after
  a successful removal. `/inventory` rewritten: splits role items
  from consumables, shows equipped role, attaches
  `InventoryEquipView` (a Select menu) when the member owns any role
  items. New `InventoryEquipSelect`/`InventoryEquipView` classes.
- `cogs/minigames.py` — added `get_user_win_count()` (free query
  against existing `minigames_log`, no schema change).
- `dashboard/api/economy_shop.py` — `add_shop_item()` accepts
  `icon_url` + `rarity`, upserts into `item_catalog` with the item's
  price as the tiebreaker value. `shop_items_partial()` now shows the
  icon + a rarity badge in the admin table.
- `dashboard/templates/systems/shop.html` — added Icon URL + Rarity
  fields to the Add Shop Item form.

All Python files `py_compile` clean. Ran a full runtime smoke test
against a scratch SQLite DB (not just compile-check): confirmed
`item_sort_key` produces the *exact* Mofasa > Lady Bug > Gumball
ordering Dark specified, confirmed `set_quantity`'s metadata COALESCE
doesn't get wiped by a re-purchase, confirmed
`cleanup_expired_role_item` correctly clears both the inventory row
and `equipped_roles` when a temp role expires.

## Locked design decisions this pass (flagging so next session doesn't relitigate)

1. **Rarity is the primary sort key, price is a tiebreaker.** Solves
   "minigame drops are technically free" — a free Legendary drop
   outranks a purchased Common item.
2. **Buying/winning a role/temp_role auto-equips it**, swapping out
   whatever was previously equipped. This matches the *existing*
   behavior (the Discord role has always been granted immediately on
   this reward path) — this pass just routes that grant through the
   shared swap instead of a raw `add_roles()` call, so it also stays
   correct in `equipped_roles`/inventory.
3. **Single-equipped-slot applies to EVERY reward_type
   role/temp_role grant** — shop, events, minigames, missions, tag
   missions, tag partners — not just shop purchases. All of those are
   "you got a cosmetic role as a reward" in the same sense.
4. **The Rank Card has zero connection to the equip system** —
   equipping/swapping never touches, never displays anything about
   equip history on the card. Confirmed explicitly by Dark.
5. **No dashboard catalog editor for non-shop items this pass** — an
   event/minigame/mission-only item has no `item_catalog` row until
   an admin manually adds one (future work); falls back to
   `rarity='common'`, no icon. Per Dark: "not now."
6. **Custom title/rank name: not being added.** Confirmed by Dark.

## NOT done this pass (deliberately deferred, per agreed order)

- **The actual `/rank` card visuals.** Pillow renderer, layout,
  mailbox art compositing, avatar ring, rarity-colored borders/
  badges on grid tiles, equipped-role slot rendering. This is next
  session. `utils/rank_card_data.py` already has everything it needs
  to consume.
- No dashboard route/UI change for `/rank` itself yet (still the old
  embed-or-Pillow-fallback in `cogs/leveling.py` — untouched this
  pass).
- No admin catalog editor for minigame-tier / mission-reward items
  (rarity/icon only settable via the shop item form right now).

## Still needed, in order

1. Restart the bot once so `database.py`'s `init_db()` runs the 3 new
   migrations against the real DB.
2. Live-test: buy a role item from the shop → confirm it lands in
   `/inventory` under "Owned Roles" and shows as equipped. Buy a
   *second* role item → confirm the first one's Discord role is
   actually removed and the second is added (the swap).
3. Live-test: buy a `temp_role` item with a short duration, wait for
   `temp_role_cleanup` (10 min loop) to fire → confirm the item drops
   out of `/inventory` and `equipped_roles` is cleared if it was
   equipped.
4. Set icon + rarity on a couple of real shop items via the dashboard,
   confirm the admin table shows the icon + colored rarity badge.
5. **Next session: the `/rank` card visual build.** Needs from Dark
   at that point: nothing new — rarity colors, sort logic, and all
   data plumbing are already locked and built. Just start it.
