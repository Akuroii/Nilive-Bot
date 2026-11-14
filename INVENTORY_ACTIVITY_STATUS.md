# Inventory and activity integration

Base: `043033bb5a82353bb4072ad2b4d74af0236d45cd`. This document does not authorize deployment. No historical backfill is performed; the History panel reports the recording start and partial coverage. Wallet Receipts remains a separate financial ledger.

## Recorded mutations

Events are inserted on the same SQLite connection and transaction as the underlying write. A failed event insert fails the associated mutation; this is a deliberate fail-closed change to failure behavior. The log records established writes, not a guarantee that an external business operation finalized.

* Permanent inventory increments through `give_item`, including Level restoration; Shop acquisitions mean delivered inventory, not finalized purchases. Internal potion refunds are excluded.
* Guarded purchase-grant reversals and applied Level inventory reversals. (Member stack drops were recorded as `dropped` until the Drop feature was removed on 2026-11-13; those existing rows remain in History, no new ones are written.)
* Trade item legs, in the trade transaction, with distinct keys per side and direction.
* Timed holding grants, ordinary expiry, and the identified late-compensation holding removal; internal retention and late-rollback retry markers are excluded.
* Potion consumption/effect creation together; role/title slot changes recorded with slot writes. Role reward inventory writes and their actual rollback are recorded separately.

Keys deduplicate named transaction-bound operations, not arbitrary repeated source deliveries. Mission, Event and other source calls that grant twice can still actually deliver twice. A role slot event records the SQLite change; Discord may fail or become uncertain independently. Direct `set_quantity` staff adjustments, legacy temporary-role inventory cleanup, some correction/automatic equipment reconciliation writes, and other direct SQL mutations remain outside the log. Source operation IDs for Mission/Event/Minigame/Tag Partner acquisitions are not yet propagated. No inferred backfill or claim of complete coverage is made.

## Ownership and role coordination

The unified reader (`get_inventory_entries`) was added in this work and is opt-in; old `get_inventory` consumers keep their permanent-only contract. The UI uses six tabs and entry keys, and supports paging. There is no member Drop: the button, its confirmation, `utils.inventory.drop_item` and the `drop_requested` path of `expire_one` were removed on 2026-11-13, so a permanent stack leaves only by use, trade or an admin/correction write and a timed holding only by expiry. Automatic expiry remains unchanged. Permanent role delivery, equip/unequip, timed grant and expiry, legacy expiry, late compensation, retention reconciliation, Level progression, trade, Twitch and Prestige role changes use the per-member gate where adapted. Two-member trade takes locks in ascending user-ID order before entering SQLite.

The gate is process-local, assumes a single active bot event loop, and retains its lock registry; it cannot protect a second process or unrelated direct database writers. Discord and SQLite cannot be made one atomic transaction: a cancellation, uncertain network failure, or a concurrent non-cooperating writer during a Discord await can still require reconciliation. Late purchase compensation is evidence-based and refuses unproven legacy goods. Role-delivery snapshots and other source-specific semantics need further adversarial verification. Permanent duplicate Shop role purchases are refused rather than charging for an unchanged grant.

`record_purchase_grant`, `purchase_grant`, `reverse_purchase_grant`, their schema/index and transaction boundaries, `rollback_role_inventory_grant`, equipment bookkeeping helpers, and the offline purchase-recovery CLI were selectively recovered from PR #107; newer main ownership, purchase journal, Product Management, member blacklist and other systems were retained. The UI/emoji design and focused regression scenarios were selectively adapted from #104/#107, not merged wholesale.

The full requested regression matrix and live Discord verification are not yet complete. In particular, category/ownership mutation paths, all role-removal callers, equip race behavior across unrelated writers, legacy cleanup retry gaps, and the precise activity coverage must be reviewed before release. Do not deploy without explicit approval.

## Item identity on grant (2026-11-13)

`inventory_items` is one row per `(guild_id, user_id, item_name)`, and equip slots, trades, purchase grants, level provenance and reversals all address a permanent stack by that name. `give_item` therefore never changes the type of an owned stack: the same type stacks; two plain collectible types (`custom`, `shop_custom`, `event_drop`, ...) stack and the stored type stays; a grant with no type (the Level-correction restore) keeps the stored type; and a `potion`, `title`, `role` or `temp_role` meeting a different kind raises `ItemTypeConflict` and writes nothing ("An unrelated item already uses this name", the wording the permanent-role path already used). A row left at quantity 0 is not owned and may be reused. No schema change. Timed holdings are unaffected - they already have their own table and entry keys. Covered by `scripts/test_prestige_rank_and_item_identity.py`.

Prestige is not part of the Inventory: it is one rank per member (`levels.prestige`, I-V) plus the Booster-only VI activation, and the tier in effect is derived by `utils.prestige.get_effective_prestige`. There is no Prestige equip slot.

## Reward names and undelivered rewards (2026-11-13)

**Creation time.** `utils/item_identity.py` is the one check for "may this guild define an item with this name". Kinds are potion, title, role item (Shop `role` / `temp_role` / `color`) and plain item (Shop `custom`, and every Mission / Event / Minigame `item` reward). A name may be shared within a kind and between plain items; any other pairing is refused, per guild and by the exact stored name. It also counts stacks members already own, so a renamed or deleted listing still reserves its name while someone holds the item. It is called from Shop product create and rename (`dashboard/api/economy_shop.py`), `mission_engine.create_definition`, `/event_create`, and every Minigame reward list (`minigame_store` template pools, category defaults, global defaults). Level rewards reference Shop products by id, so the Shop check covers them. Not covered at creation: `role` / `temp_role` rewards of Missions, Events and Minigames, which are stored under the Discord role's name at delivery time.

**Runtime.** `give_reward` never drops an `item`, `role` or `temp_role` reward the Inventory refuses over a name clash. It writes a `pending_rewards` row (new table, `utils/pending_rewards.py`) and returns `success=False, pending=True`. The mission stays completed, the round resolved, the winner recorded. Shop purchases are not parked (definite failure -> refund) and Level claims keep their own `failed` -> retry flow.

States of a held reward:

| State | Meaning | Who moves it on |
|---|---|---|
| `pending` | Stored, waiting for the name to be free | The scheduler loop (every 60 s) and the member opening `/inventory` |
| `delivering` | One retry has claimed it (guarded `UPDATE ... WHERE status='pending'`), so concurrent retries deliver once | That retry: -> `delivered`, back to `pending` if still refused, or -> `needs_review` |
| `delivered` | Delivery confirmed. Final | Nobody |
| `needs_review` | The outcome is unknown: the delivery reported `uncertain`, raised unexpectedly, or the bot stopped mid-delivery (a `delivering` claim older than 10 minutes). **Never retried automatically** | Staff only, on the dashboard Inventory page: "Not received - retry delivery" (-> `pending`) or "Received - mark as delivered" (-> `delivered`). `POST /api/pending-rewards/<id>/<retry|delivered>`, admin level, audited |

Notices. The member is told when the reward is held, once, by the code that completed the objective (retries call `give_reward` only and announce nothing): a Mission sends a DM, or posts in the activity channel if DMs are closed; a Minigame adds a line for the winner to the result embed and keeps the winner as `won` in the run log; the Events button race answers the click; a tag mission sends a held notice instead of its success or failure text. The Inventory keeps showing a "Reward waiting" note until delivery, and staff see every held reward with its state, source, reason and attempts on the dashboard Inventory pages.

**Open design decision - metadata of an owned stack.** A same-type grant still replaces the stack's metadata (`COALESCE(excluded.metadata, existing)`), so potions a member already owns take the multiplier and duration of the most recent grant under that name. This is unchanged existing behaviour. Whether owned items should follow later changes to their definition or keep the properties they were acquired with needs a separate decision; it would mean per-unit or per-version storage and is deliberately not part of this work.

**`set_quantity`** still writes `item_type` unconditionally. Its only caller is the permanent-role grant in `utils/reward_engine.py`, which refuses a same-name stack of any other kind or other role before calling it (covered by `PermanentRoleGrantCollisionTests`).

**Root-level `wallet.py` / `inventory.py`** are older flat copies that nothing imports (see MANIFEST.md, "root-level duplicates"). Their Drop code was removed on 2026-11-13; the rest of each file is left as it was.

## Verification in this working tree

The runs below predate the 2026-11-13 Drop removal and are kept as the historical record. `test_inventory_timed_drop.py` is now `test_inventory_timed_expiry.py` (same Discord-failure, entitlement and cancellation scenarios, driven through real expiry); the Drop-specific cases in the other suites were removed or retargeted, and `scripts/test_inventory_actions.py` covers Equip/Unequip, potion activation and the removal itself.

Scratch SQLite and Discord stubs, not a live Discord server. Final timed-Drop policy checks: Inventory Activity and UI suites 34 passed; full timed ownership 241 passed with 5 subtests; race, purchase, retention and delivery suites 86 passed with 4 subtests; migration 9 passed with 2 subtests; Wallet 73 and 54 checks passed; Rank integration passed with CDN warnings.

Earlier acceptance run:

* `test_timed_ownership.py`: 241 passed, 5 subtests passed.
* Inventory Activity, keyed action and six-tab UI scripts under pytest: 30 passed. `test_inventory_timed_drop.py`: 5 passed, covering internal timed removal/reconciliation, Discord failure/retry, cancellation, expiry, and duplicate paid permanent role refund; member-facing timed Drop is now disabled.
* #107 race, purchase-grant, retention, delivery and timed Drop suites: 86 passed, 4 subtests. Adding PR #105 fixes produced 160 passed, 20 subtests and 5 failing negative-creation subcases: current main accepts incompatible, unused fields on product creation, whereas #105 expects rejection. An extracted copy of the same negative-validation class against the exact base produced the same five failures. Tightening that public creation contract also breaks existing Shop API expectations, so this review leaves the existing main behavior intact rather than choosing that policy change silently. Migration concurrency: 9 passed, 2 subtests.
* Wallet scripts: 73 and 54 checks passed. Rank integration: five cases passed with CDN image-fetch warnings due to the sandbox network. Level role progression: 46 checks passed after making its crossing fixture update XP and implementing Discord member lookup; Level test reset passed after recognizing the new activity table. Level reward end-to-end and five XP caller integrations passed. Missions v2: 149 checks passed; Tag Missions save: 10 tests passed.
* Root boost/mission/level/claim pytest group: 10 passed. Trade, title and potion action coverage is included in focused Activity and timed suites, not a claim of exhaustive integration coverage.
* Shop free/category/VI API suites: 43 passed, 7 failed/subfailed; comparison with the base checkout produced the same failures plus one additional XP-boost subfailure (43 passed, 8 failed/subfailed). These older expectations conflict with existing timed storage, XP-boost creation restrictions, or VI/title input rules. Shop options/design/publication/publisher/select/JSON suites: 74 passed, 2 failed, 31 subtests passed; the same two failures occur on base (catalog/query expectations). The rank root file has no pytest tests; its script invocation was used instead.

The transactional financial ledger now propagates insert errors to a caller-owned transaction instead of committing money/stock without its ledger. Standalone best-effort logging retains the prior behavior. Tests and UI mocks do not establish actual Discord permissions, live gateway ordering, deployment migration safety, multi-process role serialization, or exhaustive coverage of all reward sources.
