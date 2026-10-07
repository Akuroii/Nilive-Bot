# Leveling follow-up audit — anti-spam penalty, leave/rejoin, blacklist (items 3–5)

> Historical audit/evidence notes. Early leave/rejoin and anti-spam descriptions below predate the current join-callback reconciler and persistent rolling penalty cap; the resolved behavior is summarized above and in §§4D–4E. Current production code/tests are authoritative.

**Decisions resolved on 2026-10-06 (second pass).** D1 = **B** and D2 = **B** are implemented —
the anti-spam penalty is a real deduction that stays floored at zero with the level recomputed, and
`reconcile_role_progression()` restores fulfilled Level roles from the claim ledger on member join
(OFF restores all accumulated roles; ON restores the highest fulfilled set) and on the appropriate
Claim All pass. D3 is **not implemented**: the member blacklist system is still under construction,
so nothing blacklist-related was wired (see §5.3). The audit below is kept as the evidence trail for
those decisions; sections that described the pre-fix behaviour are marked.

Evidence comes from the code itself plus
[`scripts/simulate_negative_xp.py`](scripts/simulate_negative_xp.py), which prints every number
below and fails loudly if the production shapes it models drift (`python3 scripts/simulate_negative_xp.py`
from `scripts/`).

What was implemented after the decisions:

| Item | Implemented behaviour |
|---|---|
| 3 (D1 = B) | `apply_spam_penalty()` in `utils/xp_calculator.py`: subtracts the penalty from current XP, floors it at zero (no debt, XP never negative), recomputes `level` from the resulting XP in the same transaction, writes no entitlement and no role change. The cog calls it from the existing spam branch and then replies once per spam window with the approved anti-spam embed (`SPAM_WARNING_TEXT` — a reply to the offending message, reporting the XP that was actually deducted; the penalty applies to every spamming message, the visible warning does not). `spam_threshold` / `spam_xp_penalty` / `spam_window_seconds` are untouched. |
| 4 (D2 = B) | `reconcile_role_progression()` in `utils/level_claims.py` is shared by the member-join listener and the member's full Claim All pass when replacement is ON. It is membership-only: OFF restores every fulfilled role claim because roles accumulate; ON restores the highest fulfilled set and removes lower fulfilled roles only after that set is present. It never changes XP or the claim ledger, and reports Discord failures without reopening fulfilled claims. |
| 5 (D3) | **Not implemented, deliberately.** No blacklist code, table, command or automatic restore/unrestore was added. The future invariant is documented in the code (`utils/level_claims.py`, both the delivery loop and the reconciler) and in §5.3 below. |
| retroactive sweep | **Not implemented.** No background task or guild-wide pass. Membership is reconciled for an individual on join, or through the member's qualifying full Claim All pass; there is no bulk migration or sweep. |

---

## Item 3 — Anti-spam penalty: floor-at-zero vs subtract-from-current-XP

### 3.1 Every XP write path, and whether XP can go negative

| # | Path | Code | Floor | Level recomputed |
|---|---|---|---|---|
| 1 | XP grant (message / voice / direct reward) | `utils/reward_engine.py:155` `new_xp = max(0, old_xp + amount)` | yes | yes — `xp_progress(new_xp)` |
| 2 | **Anti-spam penalty** | `cogs/leveling.py:418` `SET xp = MAX(0, xp - ?)` | yes | **no** |
| 3 | Admin `/setxp` | `cogs/leveling.py:693` `xp = max(0, xp)` | yes | yes |
| 4 | Admin `/resetxp` | `cogs/leveling.py:743` `SET xp = 0, level = 0` | n/a | yes |
| 5 | `/resetleaderboard` + scheduled reset | `cogs/leveling.py:84` `SET xp = 0, level = 0` | n/a | yes |
| 6 | Dashboard member edit | `dashboard/app.py:2656` `max(0, int(...))` | yes | yes — `calculate_level_from_xp` |

**Answer: XP cannot go negative anywhere in the shipped model.** Paths 1, 2, 3 and 6 clamp; 4 and 5
write zero. `xp >= 0` is assumed by `xp_progress()`, `calculate_level_from_xp()` and the rank card —
`/setxp` carries an explicit hardening comment recording that assumption.

### 3.2 What the shipped penalty actually does (simulated)

* **Floor holds.** 12 consecutive penalties of the default 10 XP against a member at 0 XP leave
  `xp = 0` every time: no debt, no negative state, no reset needed. This is the behaviour the user
  asked to preserve, and it is intact.
* **Latent defect (now fixed) — the stored level could go stale.** Before this slice the penalty
  decremented `xp` without recomputing `levels.level`, and it was only harmless for a member near a
  level floor. Simulated on a member 3 XP into Level 2 (`xp = 385`, Level 2 starts at 382):

  | Step | xp | `levels.level` | `xp_progress(xp)` level |
  |---|---|---|---|
  | start | 385 | 2 | 2 |
  | penalty 10 | 375 | **2** | **1** |
  | next message (+5 XP grant) | 380 | **1** | 1 |

  That write left `levels.level` disagreeing with the XP curve, and the *next legitimate XP grant*
  recomputed the level from xp — so the member silently dropped from Level 2 to Level 1 on a message
  that granted XP, with `leveled_up = False` and therefore no announcement. **Fixed:** the shipped
  `apply_spam_penalty()` floors the XP *and* recomputes the level in the same transaction, so the
  demotion happens with the penalty (deliberately) and never as a surprise on the next message.
* **Reward integrity is not affected by it.** `record_crossing()` only counts level *increases*, and
  a decrease adds no claim; delivered claims keep their roles. With `remove_old_reward_role` ON,
  nothing is removed on the way down either (enforcement only runs after a delivery).

### 3.3 What "penalty subtracts from current XP" (no floor) would change

Simulated from 0 XP with the default penalty: `xp` goes `-10 → -20 → -30`.

| Surface | With a floor (today) | Without a floor (proposed) |
|---|---|---|
| `xp` value | never below 0 | negative, unbounded downward with repeat offences |
| `xp_progress(xp)` | `(level, remaining, needed)` all valid | `xp_progress(-30)` → level 0, **remaining `-30`**, needed 100 → the rank card renders a **-30 % progress fraction** |
| `calculate_level_from_xp(-30)` | 0 | 0 — safe |
| Cumulative curve | unchanged | unchanged; the member simply **owes** 130 XP before Level 1 (`100 − (−30)`) |
| Level reward claims | unaffected | unaffected (a decrease creates no crossing) |
| Re-earning a level | — | **cannot pay twice**: `UNIQUE (guild_id, user_id, reward_level, track, reward_ref)` + `INSERT OR IGNORE` (`utils/level_claims.py:36`, `:233`) |
| Leaderboard | 0 XP members tie at the bottom | the spammer sorts **below every 0-XP member** (`ORDER BY xp DESC`) — a rank the UI never explains |
| Ledger / reset | — | `xp_transactions` rows would carry negative `new_xp`; reset to 0 restores a clean state |

The duplicate-reward exploit was simulated explicitly: crossing into Level 1, being penalised below
the threshold, re-crossing, being penalised again and re-crossing a third time leaves **one** claim
row in the ledger. The re-crossings do print `1` from `record_crossing()`, but that return value
counts the identities *attempted*, not the rows inserted — no production caller reads it
(`cogs/leveling.py:715`, `dashboard/app.py:2679`, `utils/reward_engine.py:166` all ignore it).

### 3.4 Decision (resolved: D1 = B, implemented)

**Chosen: (b).** The penalty is a real deduction that is floored at zero, and `level` is recomputed
from the resulting XP in the same transaction. Negative XP is explicitly **not** allowed: no debt,
no negative progress, no rank below the 0-XP floor. A penalty may visibly demote a member — that is
intended — and delivered Level roles are not revoked by it (claims stay fulfilled; with
`remove_old_reward_role` ON nothing is removed on the way down either, because enforcement only runs
after a delivery).

The rejected alternatives are recorded here for the record: (a) keep `level` stale — rejected
because it produces the silent demotion described above; (c) allow negative XP — rejected because it
breaks the `xp >= 0` invariant the curve/rank card/dashboard assume (see §3.3).

Implementation: `apply_spam_penalty()` (`utils/xp_calculator.py`), called from the existing spam
branch in `cogs/leveling.py`. It writes no `xp_transactions` row (the penalty never did; the ledger
supports negative amounts as `type="deduct"` if that is ever wanted), and does not call
`record_crossing()`, so a demotion cannot create, revoke or duplicate an entitlement.

---

## Item 4 — Leave/rejoin determinism

### 4A. Identity and XP survive leave/rejoin — verified

* `levels` is keyed `PRIMARY KEY (guild_id, user_id)` (`database.py:130`) and remains the source of
  XP/level/prestige state. No production path deletes the row on member removal.
* The Leveling cog's `on_member_join` listener now calls `reconcile_role_progression()` using the
  persisted claim ledger. It changes Discord role membership only: the runtime test snapshots a
  nonzero XP/Level row, economy, claims, reward definitions and ledger before/after join and verifies
  they are byte-for-byte unchanged.
* Other member-lifecycle cogs do not own Level reward restoration; this listener is the explicit
  production entry point, not a guild-wide sweep.
* Consequence: a member who leaves and rejoins the same guild keeps the same `guild_id + user_id`
  progression and claims, while missing fulfilled reward roles are restored according to the
  persisted `remove_old_reward_role` setting. Only a *new Discord account* starts over.

### 4B. A role Discord removed while the member was away — restored without a new crossing

Reward entitlement and role membership are separate. A normal message reaches the claim ledger only
through the XP path `on_activity_message()` → `give_reward()` → `record_crossing()`, which creates
pending entitlements on a genuine level increase; it does not restore fulfilled roles. Restoration
uses the existing ledger through two member-scoped callers:

* **Rejoin:** `on_member_join()` calls `reconcile_role_progression()` directly. It reads fulfilled
  `role` claims only and does not reserve or pay them again.
* **Claim All:** `claim_available()` delivers pending/retryable claims through the existing delivery
  engine; its full member pass also reconciles already-fulfilled role membership when replacement is
  ON. A narrowed `claim_ids` pass does not run reconciliation.

When `remove_old_reward_role` is OFF, join reconciliation restores every fulfilled role because the
progression accumulates. When ON, it restores the highest fulfilled Level set and removes lower roles
only after the full set is present. No timer, message-triggered role sweep, or guild-wide sweep was
introduced.

### 4C. No per-message role sweep — verified

`deliver_role()` (`utils/level_claims.py`) is the shared Discord role-add helper used by the claim
engine's permanent-role and Shop temp-role loops and by `reconcile_role_progression()` for an
already-fulfilled Level role. The latter is called on member join, not on each message. The message
listener only computes XP and calls `give_reward()`; there is no message listener, timer, or
background role sweep.

### 4D. The smallest safe shape (the one D2 = B selected)

Restoration uses the existing claim architecture: a member is present, a `role` claim is fulfilled,
and a missing role is re-added via `deliver_role()` (no new engine, table, or reward type). The shared
membership-only reconciliation is invoked by the production member-join callback and by a full
Claim All pass when replacement is ON:

* only this member's fulfilled `track='role'` claims are candidates; pending/failed claims are not paid
  by rejoin;
* with `remove_old_reward_role` **OFF**, restore all fulfilled role claims because the configured
  progression accumulates roles;
* with it **ON**, restore only the highest fulfilled Level set, then remove lower fulfilled roles only
  after the whole highest set is present;
* the future member-blacklist check is not implemented; when that system exists it must gate both
  reconciliation callers and the claim-delivery loops (see item 5);
* `deliver_role()` is safe for already-held roles, and failures are reported without changing claims.

### 4E. Decision (resolved: D2 = B, implemented)

**Chosen: (b)** — implemented as `reconcile_role_progression()` (`utils/level_claims.py`), called
by the production `on_member_join` callback and by a member-initiated full Claim All pass when
replacement is ON:

* with replacement OFF, every missing fulfilled role is restored because the configured progression
  accumulates Level roles;
* with replacement ON, only the **highest** fulfilled Level set is restored; lower fulfilled roles
  are removed only after every role in that set is present;
* an unresolved same-Level claim or failed Discord add blocks replacement, so a lower held role is
  never stripped when its replacement cannot be delivered;
* unrelated roles are never candidates (only role ids in fulfilled `role` claims);
* reconciliation never writes to the claim ledger or changes XP/Level. It creates no crossing and
  never pays a reward; Discord failures are reported without failing a fulfilled claim;
* no message/timer/background reconciliation or guild-wide member sweep is used, and a narrowed
  `claim_ids` pass does not reconcile;
* the Claim All footer reports explicit claim-pass reconciliation (`claim_result_footer`); join
  callback failures are logged without changing persisted reward state.

Rejected: (c) automatic sweeps — excluded by the commission and by the "no per-member Discord role
sweep" rule.

---

## Item 5 — Blacklist interaction

### 5.1 What exists today is a *role* blacklist, not a member blacklist

| Thing | Reality in this repo |
|---|---|
| Table | `leveling_blacklist_roles` (`database.py:849`) — `(guild_id, role_id)`, an XP opt-out **per role** |
| Effect | A member holding such a role earns **0 XP**: message XP via `get_xp_multiplier()` (`utils/xp_calculator.py:16`) and voice XP via `is_role_blacklisted()` (`utils/xp_calculator.py:64`) |
| Managed from | The Leveling dashboard's blacklist-role card: `GET/POST /api/leveling/blacklist` and `DELETE /api/leveling/blacklist/<id>` (`dashboard/api/leveling.py:645/662/680`), driven by the `addBlacklist`/`deleteBlacklist` markup handlers on the Leveling page |
| Member blacklist | **does not exist** — no table, no state, no command |
| Role-removing blacklist command | **does not exist** |
| Forgiveness / unblacklist command | **does not exist** |
| Admin role manipulation requirement | nothing to remove yet |
| Lookalike #1 | `utils/command_gating.py:117/129` — a **role** blacklist and a **channel** blacklist that decide *who may use a command*, not who may earn XP. Unrelated to leveling and to members |
| Lookalike #2 | `temp_bans` (`database.py:1427`) + `cogs/moderation.py` — moderation bans/timeouts. No leveling XP effect, no role removal, no member-blacklist semantics |
| Lookalike #3 | `/api/leveling/blacklist` route names — they manage the *role* opt-out above, not members |

So the planned feature ("a blacklist command removes roles while preserving the blacklist role; a
forgiveness command restores normal state") has no implementation to integrate with — this section
maps where it must attach rather than wiring it now.

### 5.2 Member-blacklist integration remains open

There is no member-blacklist state to consult. The message listener itself does not deliver roles,
but D2 deliberately adds automatic membership reconciliation on `on_member_join`; a future
blacklist command that removes Level roles could therefore have them restored at rejoin. A full
Claim All pass can also deliver unfulfilled claims and, with replacement ON, reconcile fulfilled
roles. The current role-based XP opt-out is not a member blacklist and does not guard either path.
This is an identified future integration risk, not a feature implemented by this task.

### 5.3 Integration point for the future feature (annotated in the code, not wired)

* **Guard (must have):** a member-blacklist check consulted **before** every `deliver_role()` call —
  the clean chokepoint is inside `claim_available()`'s two role loops (`utils/level_claims.py:612`
  and `:651`), or at the top of `deliver_role()` itself. A blacklisted member's entitlement should be
  **deferred, not failed**: leave the claim `pending` (no `last_error` churn, still retryable) so
  unblacklisting can pick it up through the normal pass.
* **Unblacklist (the reconciliation the user wants):** re-running the member's pass through
  `claim_available()` re-delivers every unfulfilled claim and honours `remove_old_reward_role`. The
  limitation to surface: claims already **fulfilled** (a role delivered before the blacklist then
  removed by the blacklist command) will *not* come back this way, because `_reserve_sql()` skips
  fulfilled rows — unblacklist restoration needs exactly the D2 mechanism from item 4D, which is why
  the two items share one decision.
* **Role hygiene for the blacklist command itself:** the blacklist role must not be a role id
  configured in `leveling_rewards` (otherwise "remove Level roles, keep the blacklist role" and
  "reconcile Level roles" would fight over it). Worth enforcing at write time when the feature is
  built.
* No dashboard/runtime coupling was found that would let a blacklist change re-open claims, so no
  existing behaviour has to be repaired before the feature lands.

### 5.4 Decision (D3 — deferred by design, nothing implemented)

The member blacklist system is **still under construction and is not `leveling_blacklist_roles`**
(which stays what it always was: a role-based XP opt-out). So this slice implements **no**
blacklist integration at all: no member-blacklist table, no command, no automatic role removal and
no unblacklist restoration. The planned behaviour (blacklist command removes roles while preserving
its own blacklist role; forgiveness restores normal state; no manual admin role manipulation) will
be integrated explicitly when that system lands.

**Invariant to honour then — and the reason it is written into the code today:**

> A member intentionally blacklisted by the future blacklist system must not have automatic Level
> role reconciliation restore roles that the blacklist system intentionally removed.

When the blacklist lands, its state must be consulted before both claim-engine role-delivery loops
(permanent and Shop temporary roles) and at both runtime callers of
`reconcile_role_progression()` (member join and qualifying full Claim All). Unfulfilled claims should
be **deferred** (left `pending`) for a blacklisted member rather than failed. Until that integration
exists, automatic rejoin restoration is active and may restore fulfilled roles; the member blacklist
invariant is therefore not enforced by the current code.

Also worth enforcing when the feature is built: the blacklist role must not be a role id configured
in `leveling_rewards`, otherwise "remove Level roles, keep the blacklist role" and "reconcile Level
roles" would fight over it.

Previously listed as D3 (what an unblacklist should restore) — still open, and now a question for
whoever builds the blacklist system: the safe answer remains "the highest entitled role, through
this same pass, gated by blacklist state", never "everything they ever claimed" (which would
contradict `remove_old_reward_role` when it is ON).

### 5.5 Inspection result: bypass risks found, and exactly what remains open

**No existing implementation conflict was found** — because there is no member blacklist to conflict
with. What the inspection *did* produce is the list of places where the future system must be
enforced, and the specific ways the current code could bypass it if it is not:

| # | Risk when the member blacklist lands | Where it must be handled |
|---|---|---|
| R1 | A blacklisted member rejoins or taps **Claim All** → `reconcile_role_progression()` can restore fulfilled Level roles the blacklist command deliberately removed (ON restores the highest set; OFF restores all accumulated roles). | both reconciler callers: the member-join callback and qualifying full Claim All path |
| R2 | A blacklisted member's **unfulfilled** claims are still delivered by the same pass (`claim_available` never asks who is allowed to receive). | the two `deliver_role()` calls (`:701` in the roles loop, and the Shop temp-role loop) — claims should be *deferred* (left `pending`), not failed |
| R3 | A blacklisted member keeps earning XP from messages/voice, because the only thing that zeroes XP today is holding a configured **blacklist role**. Whether a member blacklist should also stop XP is a product question. | `calculate_message_xp()` / `get_xp_multiplier()` (`utils/xp_calculator.py`) and `is_role_blacklisted()` for voice |
| R4 | If the blacklist role were ever configured as a Level reward, "remove Level roles, keep the blacklist role" and "reconcile Level roles" would fight over the same id. | enforce at write time in the level-reward config API |
| R5 | Conflating the two systems: `leveling_blacklist_roles` **only** zeroes XP; it removes nothing, so treating it as the member blacklist today would silently change XP behaviour for existing guilds. | documentation + the guard above, not a code change |

**What remains open (explicitly):**
1. **D3 — member-blacklist integration.** Not implemented, by instruction. Nothing in this slice
   creates a blacklist table, command, state, role removal, forgiveness flow, or automatic
   restore/unrestore behaviour. The invariant (R1) is written into the code as a comment at both
   chokepoints so it cannot be overlooked when the feature lands.
2. **The blacklist system itself** is under construction elsewhere; this repo has no member
   blacklist to integrate with.
3. When it lands, the questions to answer are: does a blacklisted member still earn XP (R3)? Are
   their unfulfilled claims deferred or failed (R2)? Does unblacklisting restore the highest
   entitled Level role through the D2 pass (the previous D3 question)?

---

## Item 6 — Final completeness audit (2026-10-06, third pass)

Run as a permanent, self-checking audit: **`scripts/test_xp_safety_audit.py`** (111 checks, 0 failed),
which prints the whole inventory and locks every invariant below. It is designed to *fail* when the
code drifts, and was mutation-tested (details after the tables).

### 6.1 Every XP / mutation entry point, and its safety property

| # | Entry point | XP write | Clamp | Level kept consistent | Touches claims/roles |
|---|---|---|---|---|---|
| 1 | `utils/reward_engine.py::give_reward()` — every positive grant (messages, voice, missions, minigames, events, shop, tag partners/missions) | `INSERT … ON CONFLICT DO UPDATE SET xp=?, level=?` | `max(0, old_xp + amount)` | `xp_progress(new_xp)` | writes the entitlement snapshot it owns (by design) |
| 2 | `utils/xp_calculator.py::apply_spam_penalty()` — anti-spam | same upsert | `max(0, old_xp - penalty)` | `xp_progress(new_xp)[0]` | **none** |
| 3 | `cogs/leveling.py::setxp()` — admin `/setxp` | list item 3 | `max(0, xp)` | `xp_progress(xp)` | records a crossing (an admin grant can raise a level) |
| 4 | `cogs/leveling.py::resetxp()` — admin `/resetxp` | `xp=0, level=0` | n/a | both zeroed together | none |
| 5 | `cogs/leveling.py::perform_leaderboard_reset()` — `/resetleaderboard` + scheduled reset | `xp=0, level=0` | n/a | both zeroed together | archives to `leveling_leaderboard_history` |
| 6 | `dashboard/app.py::api_edit_member()` — dashboard member edit | same upsert | `max(0, int(...))` | `calculate_level_from_xp(xp)` | records a crossing |
| 7 | `utils/prestige.py::purchase_prestige()` | inserts a row for a brand-new member | n/a | writes `0, 0` for a new row | `DO UPDATE SET prestige` **only** — never touches xp/level |

Anything else writing to `levels` fails the audit until it is classified. There is **no
`DELETE FROM levels`** in production code.

### 6.2 The requested invariants, and where they are proven

| Invariant | Result | Evidence |
|---|---|---|
| every normal positive grant goes through the intended path | **holds** — all XP grants funnel into `give_reward()`; the missions *cog* only renders reward text, `utils/mission_engine.py` performs the payout | audit §4 + inventory |
| no path can make XP negative | **holds** — every writer clamps or writes 0 (§6.1); 5 penalties on 7 XP leave `0 XP / level 0` | audit §2, §14; `test_spam_penalty` §3/§4 |
| no path can bypass the spam protection unintentionally | **holds** — the spam gate sits before the cooldown gate and before the grant, and `apply_spam_penalty` has exactly one production caller (the message listener) | audit §5 (ordered index check + live-condition check) |
| spam cannot create duplicate rewards/claims | **holds** — the penalty body never touches `level_reward_claims`, never calls `record_crossing`, never adds/removes roles; re-crossing is absorbed by `UNIQUE(…)` + `INSERT OR IGNORE` | audit §6; `test_spam_penalty` §5/§6 |
| level and XP cannot become inconsistent | **holds** — all four XP-changing writers recompute the level in the same function as the write, and resets zero both | audit §3 (per-writer function-body check) |
| demotion cannot re-trigger a fulfilled reward | **holds** — the reserve SQL only takes `status != 'fulfilled'` rows, entitlement identity is frozen at first crossing, and `record_crossing` requires an increase | audit §7/§8 |
| Claim All cannot duplicate a reward | **holds** — lease with `owner_token`, per-claim savepoints, `_mark()` requiring the owner token | audit §7; `test_slice2/3/4` suites |
| `remove_old_reward_role` ON/OFF semantics | **hold** — OFF accumulates and join restores every fulfilled role; ON supersedes and reconciles only the highest fulfilled set | `test_level_reward_role_progression.py` 46/46; `test_rejoin_reconciliation.py` 18/18 |
| role restoration cannot restore unauthorized/unrelated roles | **holds** — the reconciler reads only the member's own **fulfilled `role` claims** (never live reward definitions); OFF restores those accumulated claims, ON targets the highest fulfilled Level set, and unrelated ids can never appear | audit §9; `test_rejoin_reconciliation.py` |
| unrelated member roles are preserved | **holds** — removals come only from `superseded_role_ids()`, and roles the member does not hold are skipped | audit §11; progression test §4 |
| rejoin does not reset XP/Level or reward state | **holds** — the Leveling join listener changes only Discord role membership; the nonzero XP/Level row, economy, claims, reward definitions and ledger are unchanged | `test_rejoin_reconciliation.py` 18/18 |
| there is no retroactive sweep | **holds** — nothing iterates guilds/members to reconcile | audit §10 |
| no background job performs D2 reconciliation | **holds** — the shared reconciler has the member-join callback and the qualifying full Claim All pass as callers; no timer, guild-wide loop or `@tasks.loop` reaches it | audit §9/§10; `test_xp_safety_audit.py` |
| the dashboard/page-script harness remains fully verified and cannot silently degrade | **holds** — 177/177 over 36 routes; the harness prints an explicit SKIPPED notice when node/acorn is missing and asserts ≥36 routes | audit §12; harness run |
| Missions / MVP / activity_stats / voice behaviour unchanged | **holds** — none of those modules references this slice's new code; voice keeps its own gates and its own payout path; the frozen settings and the XP curve are asserted unchanged | audit §13 |

### 6.3 Two things the audit clarified

1. **Voice XP is a boundary, not a bypass.** The anti-spam gate is frequency-based *message* spam.
   Voice XP arrives through its own activity tick and keeps its own gates (`enabled`,
   `voice_xp_enabled`, `require_unmuted`, `is_role_blacklisted`) before `give_reward()`. It is not
   message spam and was deliberately not run through the message penalty — this is now asserted, so
   a future change to that boundary is a deliberate act, not an accident.
2. **`record_crossing()` returns attempts, not inserts.** It returns the number of entitlement
   identities it walked (so a re-crossing prints `1`), while `INSERT OR IGNORE` adds no row. Nothing
   reads that value in production; the simulation prints both numbers so the difference is explicit.

### 6.4 Verification of this pass (all re-run, bytecode cache cleared)

| suite | result |
|---|---|
| `scripts/test_xp_safety_audit.py` | **111 passed, 0 failed**; verifies the XP writer inventory, D2 persisted-setting / OFF-vs-ON boundaries, no ledger mutation or sweep, and frozen XP/anti-spam invariants |
| `scripts/test_spam_penalty.py` | **41 passed, 0 failed** (approved embed text byte-compared, reply-not-send asserted, footer amount, one-per-window, failure tolerance) |
| `scripts/test_rejoin_reconciliation.py` | **18 passed, 0 failed**; OFF/ON API persistence, missing-role and partial same-Level retries, no XP/Level or claim changes, and idempotent joins |
| `scripts/test_level_reward_role_progression.py` | **46 passed, 0 failed** |
| `scripts/test_leveling_reset_config.py` | **17 passed, 0 failed** |
| `scripts/test_leveling_reward_e2e.py` | **11 passed, 0 failed** — Dashboard API/UI create and supported DELETE+POST replacement→SQLite definitions→production crossing→existing Claim All delivery; duplicate protection and higher-role retry |
| `scripts/test_dashboard_page_scripts.py` | **177 passed, 0 failed** (36 routes) |
| `test_slice1..4`, `test_rank_integration`, `test_phase1_api/runtime`, `npm test` | all pass (13/13 JS harnesses) |

**Honest note on a historical false alarm.** An earlier sweep reported the then-42-check
`test_level_reward_role_progression.py` as 39/42 with roles being removed in OFF mode. That was a
**stale `.pyc`** left behind by mutation testing, not a product regression. The current suite has
46 checks and passes from source; the prior false alarm is retained only as historical context.

---

## Verification run for this slice

| check | result |
|---|---|
| `scripts/test_level_reward_role_progression.py` (item 1, both toggle modes) | **46 passed, 0 failed** |
| **new** `scripts/test_spam_penalty.py` (item 3, D1 = B) | **31 passed, 0 failed** — real deduction, floor at zero, no negative XP at any boundary, level recomputed on every fixture, demotion creates no entitlement / revokes no fulfilled claim / writes no ledger row, re-earning pays once, warning once per burst and per window, penalty 0 and detection-off unchanged; mutation-tested (dropping the level recompute fails 3 checks, dropping the floor fails 4) |
| `scripts/test_rejoin_reconciliation.py` (items 4–5, D2 = B) | **18 passed, 0 failed** — real join callback, OFF restores every fulfilled accumulated role, ON restores the highest fulfilled set and replaces lower roles, missing-role and same-Level partial add failures retry safely, repeated joins are idempotent, and XP/Level/economy/claims/reward definitions/ledger remain unchanged |
| `scripts/test_leveling_reset_config.py` | **17 passed, 0 failed** |
| `scripts/test_dashboard_page_scripts.py` (item 2, **all 36 routes** incl. `/backups`, `/tag-missions`, `/tag-partners`, `/moderation`) | **177 passed, 0 failed** |
| `scripts/simulate_negative_xp.py` (item 3) | runs clean; every assertion holds; numbers above are its output, now including the shipped write (`stale = False`) beside the legacy one |
| `test_slice1_leveling_gate.py`, `test_slice2_level_claims.py`, `test_slice3_shop_claims.py`, `test_slice4_boost_claims.py` | all pass |
| `npm test` (13 JS harnesses) | 13/13 pass |
| `test_phase1_runtime.py`, `test_phase1_api.py`, `test_rank_integration.py` | pass |

Honest note on two earlier "sandbox failures": `test_slice1_leveling_gate.py` was failing in this
environment because **Pillow was not installed**, not because of a code defect; with
`Pillow 12.3.0` present the suite passes. The `DISCORD_TOKEN`-less environment only affects suites
whose fakes need a real socket, and none of the leveling checks does.

## Not changed by this slice

The XP curve, per-message clamps, and 10-second Message XP cooldown are unchanged; the approved
anti-spam defaults remain `spam_threshold=10`, `spam_window_seconds=20`, and
`spam_xp_penalty_divisor=1000`. `/resetleaderboard` stays UPDATE-only; S4 voice is still deferred;
`levelup_embed_data` is untouched; Claim All retains its existing claim/delivery semantics; no
Shop/Prestige/Missions/MVP change; no second reward engine; no guild-wide or background sweep; no
blacklist code, table or command. The `remove_old_reward_role` schema/API setting is unchanged; the
membership reconciler now restores every fulfilled role on join when OFF and the highest fulfilled
set when ON.
