# Role Replacement — Pre-Implementation Audit

**Baseline:** `main` @ `26ce84ff30c8969ef81e729e6cb1693099d6f4cb` (merge of PR #143)
**Audited:** 2026-11-28 · branch `arena/82ef0eee-nilive-bot`
**Scope:** every existing system that grants, owns, or removes a Discord role, plus the
Dashboard configuration/authorization pattern the new surface must follow.

This document is the mandatory audit that precedes implementation. It records what each
role-owning system treats as proof of ownership, where a blind removal would break that
proof, and the design constraints that follow. **No production behaviour is changed by
this file.**

---

## 1. Ownership map — who may claim a role, and what proves it

| System | Config table | Per-member proof of grant | Remover today | Guard used before removal |
|---|---|---|---|---|
| Level rewards | `leveling_rewards` (+`audience`) | `level_reward_claims` row `status='fulfilled'`, `payload_json.role_id` | `enforce_role_progression` / `superseded_role_ids` (`level_claims.py:455,484`), only when `leveling_config.remove_old_reward_role=1` | `role_removal_blocked(retiring="level:member"|"level:booster")` (`utils/level_claims.py:767,1119,1675`) |
| Booster reward | `boost_config.boost1_role_id/boost2_role_id` | live `member.premium_since` (+ per-member count when the object exposes one) | `Boost._handle_unboost` (`cogs/boost.py:190`) | `role_removal_blocked(retiring="booster")` (`cogs/boost.py:236`) |
| Booster colours | `boost_color_roles` | active boost with `requires_boost_level` | `/boostcolor` swap, unboost, Shop expiry | `other_role_entitlement(exclude=...)` via timed-ownership paths |
| Prestige | `prestige_roles(tier)` | effective tier > 0 (`utils/prestige.get_effective_prestige`), VI activation session | `sync_prestige_roles` (`utils/prestige.py`, cosmetic sync of tier ≠ effective) | `role_removal_blocked(retiring="prestige:<tier>")` (`utils/prestige.py:635`) |
| Inventory / Equip | `inventory_items` (`item_type` `role`/`temp_role`, `metadata.role_id`) | owned item row **and** `equipped_roles` slot (`utils/equip_engine.py:68,1160`) | `unequip_role`, `swap_and_record_equipped`, expiry | `_equipped_role_linked_item_entitlement` (`utils/timed_ownership.py:810`) |
| Timed ownership | `timed_holdings` (`item_type` `role`/`temp_role`) | unexpired `expires_at` (`utils/timed_ownership.py:538`) | `expire_one`, `cleanup_expired_holdings` | `role_still_entitled` (`utils/timed_ownership.py:1425`) |
| Legacy timed roles | `temp_roles` | unexpired `expires_at` | `cogs/shop.py:1821 temp_role_cleanup` (10-min loop) | `role_still_entitled` under `slot_lock` |
| Reaction roles | `reaction_roles` (+`booster_only`, `required_role_id`), `reaction_role_expiry` | button click; **expiry row only when a duration was configured** | `RoleButton` toggle, `expiry_check` (`cogs/reactionroles.py:161`), required-role cascade (`reactionroles.py:68`) | none — removes only its own toggle/expiry |
| Twitch live | `twitch_config.give_role_id` | `enabled=1 AND is_live=1 AND discord_user_id` | `_go_offline` (`cogs/twitch.py:319`) | `role_removal_blocked(retiring="twitch")` |
| MVP | `mvp_config.mvp_role_id` | most recent `mvp_history.cycle_end` | rollover (`utils/mvp_engine.py:260`) | `role_removal_blocked(retiring="mvp")` |
| Shop (permanent role product) | `shop_items.role_id` + sold item rows | inventory/timed holdings; provenance in `reward_grants` | refunds / reversals only | `shop_role_still_owned` (`utils/timed_ownership.py:1531`) |

### 1.1 The single authoritative gate

`utils/timed_ownership.py` already contains the cross-system answer:
`role_still_entitled(guild, user, role)` → human-readable reason to **keep**, or `None`.
It consults, in order:

1. another unexpired `timed_holdings` row for the role,
2. an unexpired legacy `temp_roles` row,
3. a live `inventory_items` `role`/`temp_role` marker,
4. an equipped role-linked item (`_equipped_role_linked_item_entitlement`),
5. `non_slot_role_entitlement` → fulfilled Level claim → effective Prestige/Booster
   (`member_managed_role_ids`) → active Twitch live session → current MVP award.

`other_role_entitlement(..., exclude=...)` answers the sharper question "does a grant
**other than the one I am retiring** still justify this role", and
`role_removal_blocked(retiring=<grant token>)` is the wrapper every existing remover uses.
`GRANT_TOKENS` (`utils/timed_ownership.py:1642`) only knows grants the systems
themselves issue (`level:member`, `level:booster`, `booster:reward`, `booster:color`,
`prestige:<tier>`, family tokens). **An unknown token raises `ValueError` on purpose**
(`_retiring_tokens`): a new remover cannot silently degrade to "does the Shop own it?".

### 1.2 What this means for Role Replacement

Role Replacement **grants nothing**. It removes a role that some *other* system may still
grant, solely because an administrator declared that role subordinate to the keep role.
Therefore:

* No `retiring=` token applies. There is no grant of ours to exclude, and inventing one
  (`retiring="role_replacement"`) would have to be added to `PASS_THROUGH_TOKENS`, whose
  documented meaning is "the source is already invisible by the time removal runs" — that
  is false here and would be a semantic change to the ownership module.
* The correct gate is the **unexcluded** question: `role_still_entitled(...)`, i.e. "may
  any recorded grant still justify this role?". If yes → the role stays.
* **Gap found:** `role_still_entitled` does *not* consult `reaction_role_expiry`. A member
  who claimed a timed reaction role has a recorded expiry obligation the helper cannot
  see. `cogs/reactionroles.py` writes that row only when `expiry_days > 0`
  (`reactionroles.py:312`), so an **untimed** reaction role leaves no database trace at
  all — the grant exists only as the Discord role on the member. There is no record the
  feature could consult, and inventing one would mean rewriting the reaction-role grant
  path (out of scope). Consequences, both implemented:
  * a conflicting role with a live `reaction_role_expiry` row is kept;
  * the dashboard reports, per rule, which conflicting roles a reaction-role panel
    currently grants, so the admin is told that untimed reaction roles on those roles
    will be treated as replacable (they carry no ownership record) instead of being
    silently surprised.
* **Second gap:** `reaction_roles.required_role_id` makes a role a *gate* for other
  roles. Removing it (even legitimately, from our feature's point of view) fires
  `cogs/reactionroles.py`'s cascade and strips the dependent roles from the member. That
  is another system acting on its own ownership rules because of us; the audit flags it
  as a removal blocker so the feature cannot cause a remote cascade.

---

## 2. Level rewards — why `remove_old_reward_role` must not be reused

`remove_old_reward_role` exists for one purpose: when **a higher Level reward is
delivered**, retire the **superseded fulfilled Level claims** of the same audience
(`superseded_role_ids`, `level_claims.py:455`). It is:

* scoped to `level_reward_claims` rows (a ledger with `fulfilled` as terminal state),
* audience-split (`level:member` vs `level:booster`),
* `UNIQUE(guild_id, user_id, reward_level, track, reward_ref)` keyed,
* guarded by `role_removal_blocked`, so a role another system still grants survives,
* **only** invoked from claim delivery / reconcile (`reconcile_role_progression`), never
  from a configuration save (`cogs/leveling.py:2001`, `main.py` cog list).

Reusing or broadening it for Role Replacement would (a) couple a generic rule engine to
the reward ledger, (b) imply Level level ordering to rules that have nothing to do with
Level, and (c) risk retiring claims the member is still owed. **Decision: no reuse, no
change to `level_claims.py`.**

## 3. Prestige / Booster

* Prestige roles are removed only when the *effective tier* changes; the sync is explicit
  about never touching a role another source owns.
* Booster roles require a live `premium_since`. `_booster_level_for` documents that a real
  `discord.Member` exposes **no per-member boost count**; a boost2/colour role already
  worn is kept when eligibility is *unknown*, and a durable `role_retention` obligation is
  written (`record_role_retention`) so a restart cannot lose the decision.
* Consequence for the feature: a conflicted role that is also a configured booster/prestige
  role must not be removed while the member still qualifies — covered by
  `role_still_entitled`.

## 4. Inventory / Equip

`equipped_roles` has a PK on `(guild_id, user_id)`: one equipped role per member. The swap
path (`equip_role`, `swap_and_record_equipped`) removes the previous role **and** rewrites
the slot row, all under `slot_lock`. A new remover that deletes a role without touching
`equipped_roles` would leave the slot pointing at a role the member no longer has, and the
next swap would then try to remove it again (harmless) but `/inventory` would lie in the
meantime. **Decision:** any removal performed by the new feature must run under
`slot_lock(guild_id, user_id)` and must not remove a role the slot still entitles
(`role_still_entitled` already covers this through `_equipped_role_linked_item_entitlement`).

## 5. Reaction roles & timed ownership

Both are retry-marker designs: a row is deleted **only after** the Discord removal is
confirmed (`reactionroles.py:161`, `cogs/shop.py:1821`). The audit adopts the same rule:
the new feature keeps a durable pending row until a re-read of the member proves the role
is gone. A role that a pending removal still owes is never re-removed by the same feature
(uniqueness on `(guild, user, role)`), and `role_still_entitled` is re-checked at retry
time so a grant that arrived in between wins.

## 6. Dashboard configuration, auth, CSRF, guild isolation

* Pages: `@require_page("<key>")` + `PAGE_PERMISSIONS` in `utils/permissions.py:151`.
  Missing keys default to **owner**, so the new key must be added explicitly.
* APIs: shared `api_bp` (`/api`, `dashboard/api/__init__.py`), `@require_api_permission`
  with a level; `before_request` enforces `X-CSRF-Token` for every non-GET, and
  `dashboard/app.py:117` rejects a stale `X-Guild-ID` with 409 / malformed with 400.
  `dashboard.js` injects both headers on `fetch` and htmx requests, so page code needs no
  manual token handling.
* Guild isolation: every query is keyed by `get_session_guild_id()`; the API must never
  accept a guild id from the request body.
* Role validation: `dashboard.utils.role_validation.validate_assignable_role` is the
  established gate (existence, @everyone, managed, Administrator permission, bot
  hierarchy, tri-state bot status). It is delta-aware (`existing_role_id` preserved).
  **Unknown bot status must fail closed**, matching its `bot_status_unknown` branch.
* Style: config page + `/api` CRUD (`dashboard/api/leveling.py` is the closest model);
  `log_action` for the audit trail; `nero-role-picker` for role selection.
* Startup: `dashboard/app.py:98` calls `init_db()` at import, and the bot calls it from
  `main.py:452`, so schema creation in `init_db()` reaches both processes. The bot reads
  rules from the database on every event → **no restart and no cache invalidation needed**.

## 7. Runtime placement

* Listener cogs with `on_member_update`: `auditlog`, `boost`, `reactionroles`. A new
  `cogs/rolereplacement.py` is additive; `main.py`'s `load_cogs()` list is the only
  registration point. No slash commands → no command sync change.
* Periodic work uses `tasks.loop` + `ensure_started` + `before_loop`/`wait_until_client_ready`
  (`utils/task_lifecycle.py`), the same contract as `cogs/shop.py:1821`.
* Ordering hazard: Discord dispatches `on_member_update` to every listener concurrently.
  Our listener must tolerate a `before`/`after` snapshot that another listener has already
  mutated, hence the re-fetch of authoritative member state before removing.

## 8. Findings that constrain the design

1. **Role Replacement cannot distinguish "the admin wants B gone" from "another system
   still owes B".** Removing B because A arrived is exactly the operation the ownership
   layer exists to block. There are only two coherent policies:
   * **Strict (recommended):** skip removal whenever `role_still_entitled` (plus the
     reaction-role checks above) reports a grant. The feature then only replaces roles
     whose grant has genuinely ended or was never recorded by a Nilive system (manual
     staff assignment, external bot, deleted source). Silent, irreversible loss of a
     Level/Shop role becomes impossible.
   * **Admin override:** a per-rule "override ownership" switch, default OFF, that lets
     the rule remove a role another system still grants. This is a deliberate policy
     change to legacy ownership (a fulfilled Level claim is terminal and will **not**
     re-grant), so it needs explicit approval and separate tests.
2. Removing a role that is a `reaction_roles.required_role_id` triggers that panel's
   cascade and strips dependent roles. Treated as a blocker in the strict policy.
3. One migration is necessary: there is no existing table that can express
   "keep role A vs conflicting role B" (config) or "removal owed for member M" (retry
   marker). Both are additive `CREATE TABLE IF NOT EXISTS` statements; no existing column
   or row is touched.
4. Saving a rule must never scan members. The design has no guild-wide pass at all:
   rules are evaluated only from `on_member_update` additions and from the retry sweep
   for removals already attempted.
5. No cascade: the engine acts only on roles present in `added` (plus the conflict roles
   held when a keep role arrives). A removal produces an update with no additions, so our
   own effects cannot re-enter the engine.

## 9. Proposed design (pending the policy decision in §8.1)

* `utils/role_replacement.py` — schema, validation (self-conflict, duplicates, cycles),
  read path (no cache), event processing, ownership gate, verified removal under
  `slot_lock`, durable pending retries, sweep.
* `cogs/rolereplacement.py` — `on_member_update` listener + 10-minute retry loop.
* `database.py` — call `ensure_role_replacement_schema()` beside the other additive
  schema hooks; `main.py` — load the cog.
* Dashboard: `/role-replacement` page (Manage section, `LEVEL_ADMIN`),
  `dashboard/api/role_replacement.py` on `api_bp` (GET/POST/DELETE/toggle),
  `dashboard/templates/manage/role_replacement.html`.
* Tests: `scripts/test_role_replacement.py` (runtime, Discord fakes, temp DB) and
  `scripts/test_role_replacement_dashboard.py` (Flask test client, CSRF, guild isolation).
