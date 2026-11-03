# Nero Bot update overlay: `nero-update.zip`

**Target:** `Akuroii/Nilive-Bot` `main` @ `701f15d` (PR #108, 2026-11-02).
**Source:** `nero-bot-delivery.zip` (SHA-256 `9c8bfe8a549759f796e5d8a5e7a294660911814e03a05a487eecda2057aa2528`), integrated selectively, not replaced.
**Contents:** 94 files (70 modified, 24 added), +15456 / -1589 lines. No file deletions. Paths are repository-relative: extract into the project root.

> This is an overlay, not a project copy. Nothing in it has been applied, committed, pushed or deployed.
> GitHub `main` was the comparison reference. Local, unpushed changes cannot be verified from GitHub.

## Before you apply it

1. **Back up the SQLite database file** (`/app/data/nero.db` on Railway). The first `init_db()` after deploy rebuilds `reaction_roles` once (below). The rebuild is transactional and was tested only on copies, but a backup is the right safeguard for any schema change.
2. Extract into the project root, review `git diff`, run the tests, then commit yourself.
3. `MANIFEST.md` lands in the project root; delete it after review if you do not want it tracked.

## Database changes (all in `database.py`, run by `init_db()`)

| Change | Existing databases | Fresh databases |
|---|---|---|
| `reaction_roles` rebuilt: `id` primary key, composite `(message_id, role_id)` key removed, columns `row_index`, `button_index`, `custom_id` added | One-time rebuild in a single `BEGIN IMMEDIATE` transaction. All rows copied in original order, row counts compared before the old table is dropped, rolled back on any failure. Safe if the bot and dashboard start together (the shape is re-checked under the write lock). Idempotent. | Created in the final shape |
| Partial unique index `idx_reaction_roles_legacy_unique` on `(message_id, role_id) WHERE custom_id IS NULL` | Created. Keeps `/rr` `INSERT OR REPLACE` replacing exactly as today, while Studio rows (which always have a `custom_id`) may repeat a role. | Created |
| New tables: `reaction_role_selects`, `reaction_role_select_options`, `embed_send_nonces`, `leveling_member_blacklist`, `leveling_blacklist_restores` | `CREATE TABLE IF NOT EXISTS` (additive) | Created |

Verified on copies only: rows and every column value preserved, integrity check OK, second run is a no-op, two concurrent starts migrate exactly once, an injected failure at the `DROP` leaves the original table intact, and a fresh database has the same shape as a migrated one. **No migration was run against your real data.**

## What is integrated

| Feature | Notes |
|---|---|
| Select-menu Reaction Roles and Embed Studio | Includes the same role on several buttons in one panel. Limitation: duplicate buttons for one role on one panel share a single expiry template, because `reaction_role_expiry` is keyed by `(role, message)`. |
| Member Blacklist | See "Blacklist behavior" below. |
| Role-assignability validation | Dashboard saves of reward, MVP, Twitch, mission, warning-threshold, Prestige and panel roles check with Discord first. **Fails closed**: if Discord cannot be reached the save is rejected. |
| `check_bot_role_position` | Now matches discord.py 2.7.1 `Role.is_assignable()`, plus a Manage Roles check. Differs from the old check only where Discord would have rejected the assignment anyway (`@everyone`, managed roles, missing Manage Roles) and for the guild-owner bypass Discord itself grants. All 18 call sites use only the `(bool, message)` result. |
| Dashboard shell redesign | Icons, tabs, context-switch page, server picker categories, Overview/Commands/Moderation pages, `?g=` stale-tab guard, `X-Guild-ID` consistency check. |
| Welcome rules-role error handling | Reports a clear message instead of raising. |
| Moderation | Quick actions store `user_display_name`; edit-reason fixed (see "Incidental fix"). |

### Blacklist behavior (how it works in production)

The dashboard is a separate process with no Discord cache, so it cannot add or remove roles. In the original ZIP the role stripping therefore only ran in tests. Here the dashboard writes the database and **the bot applies the role side** from a 2-minute loop (`blacklist_role_task` in `cogs/leveling.py`):

* **Blacklist:** XP and reward claims stop immediately. Within about 2 minutes the bot removes Level-only roles from the member.
* **Unblacklist:** the dashboard queues the member in `leveling_blacklist_restores`; the bot restores roles with the existing `restore_reached_level_roles` and dequeues only on success. A guild the bot cannot see yet keeps the row queued; a member confirmed absent (REST 404) is dropped.
* **Roles are never removed if another system justifies them:** only roles granted by a fulfilled Level claim are candidates, and a role is kept when it is recorded as owned via Shop inventory, a `temp_roles` unit, a live `timed_holdings` row (including booster-retention obligations), or is configured as the MVP, Booster, Booster-colour or Prestige role. Kept roles are reported as `kept_roles`.
* **Consequence of the provenance rule:** a role that is merely *configured* as a Level reward and held without any Level claim record is no longer removed (the original ZIP removed it).
* The dashboard messages say the bot will apply the change shortly instead of claiming immediate removal.
* Limitation: the removal sweep uses the member cache only, so it relies on the bot's members intent (already enabled).

### Incidental fix inside an included file

`dashboard/api/moderation.py` (edit-reason) no longer sets `moderation_logs.updated_at`. That column does not exist in your schema, so on current `main` editing a reason fails with `no such column: updated_at` (HTTP 500). Reproduced on `main`; fixed and covered by `scripts/test_moderation_edit_reason.py`. Say so if you would rather leave this out.

## Deliberately excluded (and why)

| Excluded | Reason |
|---|---|
| `cogs/activity_engine.py` (voice-guard rework) | Would reverse PR #93/#95 (guard OFF counts restricted minutes for missions and MVP). Breaks `test_activity_voice_startup`. `main`'s own `test_leveling_voice_hard_blocks` (last touched before #93) encodes the older contract and fails on current `main`; left alone. |
| `/rank` "no XP yet" guard removal and ZIP `scripts/test_rank_integration.py` | No functional defect demonstrated; the guard reads the same `levels` table that holds all XP and its message is accurate. Your `test_rank_integration.py` is kept. |
| All demo-login additions | `login_required` / `require_page` / `require_api_permission` auto-creating sessions, the redirects to `demo_login`, `SameSite=None`, removal of `X-Frame-Options` and the relaxed CSP in demo mode, fake demo channel list, fake moderation success without a bot token, `demo_login` `next=`/guild-name additions. Your existing `/demo-login` route is untouched. The inert `is_demo` template flag is kept. The ZIP test `test_demo_login_preserves_next_deep_link` was removed because it only tests that excluded feature; the test that unauthenticated deep links redirect to `/login` is kept. |
| `OWNER_ID` re-read in `dashboard/permissions.py` | Redundant in production: `database.OWNER_DISCORD_ID` is the same `OWNER_ID` value read at import. Adds no access and was only convenient for tests. |
| Six root-level duplicates `auth.py`, `permissions.py`, `main.css`, `nero-select.js`, `reactionroles.py`, `welcome.py` | Nothing imports or serves them; the app uses `dashboard/*`, `utils/*`, `cogs/*`. |
| `scripts/recover_shop_purchases.py`, `tools/alias_probes/run_all.sh` | Older Shop-work copy / identical to `main`. |
| ZIP's `reaction_roles` schema edit as written | Replaced by the tested rebuild above: the ZIP only changed fresh databases, so its tests never saw production's constraint, and it swallowed errors with `except: pass`. |
| ZIP `scripts/test_booster_rewards.py` second hunk | Loosened an assertion to paper over an unrelated, pre-existing drift (`followup.send` vs `edit_original_response`). Only the role-validation mock was taken. |
| Regenerated `preview_p4_*.png` | Artifacts written by the test run, not part of the update. |

### Deviations from the original ZIP (our changes on top of it)

`utils/level_claims.py` shared-ownership guard and bot-side sweep; `cogs/leveling.py` sweep loop; `database.py` rebuild instead of the ZIP's schema edit and the `leveling_blacklist_restores` table; `dashboard/templates/systems/leveling.html` honest deferred messages; `dashboard/templates/general/overview.html` label restored to `Bot Status`; plus the exclusions above. Four new test files are ours.

## Verification (isolated copies only)

Reference: unmodified `main` @ `701f15d`.

| Suite | `main` baseline | With this overlay |
|---|---|---|
| Python `scripts/test_*.py` | 62 pass / 9 fail of 71 | 76 pass / 9 fail of 85 |
| JS `scripts/test_*.js` | 18 / 18 pass | 22 / 22 pass (2 are browser tests that self-skip without Playwright) |
| Jinja templates compiled | 44 / 44 | 45 / 45 |
| Dashboard routes (GET, no params) | 105; baseline errors from missing network/tables | all 105 still present, +1 new route; 2 endpoints that raised `ConnectionError` now return 200 |

* **No regression:** no test that passes on `main` fails with the overlay. One baseline failure (`test_phase2_backend`) now passes through the ZIP's own setup fix.
* **Failures that are not caused by this update (identical output on `main`):** `test_afk`, `test_booster_rewards`, `test_free_shop_items`, `test_leveling_voice_hard_blocks`, `test_shop_emoji`, `test_shop_emoji_tls`, `test_shop_publications`, `test_slice4_boost_claims`.
* **`test_workstream1_acceptance` fails offline:** two tests make unmocked requests to discord.com. With Discord's expected 401 simulated, all 8 tests pass. It was **not** run against the real Discord API.
* **Not executed:** the two Playwright browser tests (`test_moderation_smooth_navigation.js`, `test_studio_select_ui_browser.js`) self-skip, so the Studio and navigation UI was never exercised in a real browser.
* `embedbuilder.py` and `cogs/reactionroles.py` (about 1,000 changed lines) were verified by tests, route/permission checks and targeted review, not read line by line. All other Python diffs were read in full.

## Pre-existing issues found (not fixed, outside this update)

1. **High.** `cogs/shop.py:384` calls `safe_decrement_stock(iid, db=db)` but `utils/economy_safe.py:202` takes only `item_id`. PR #108 uploaded 10 of the Shop work's files; others appear to be missing (`cogs/mvp.py`, `trade.py`, `twitch.py`, `wallet.py`, `utils/economy_safe.py`, `shop.html`). `test_free_shop_items` and `test_slice4_boost_claims` fail on `main` with this `TypeError`.
2. `test_leveling_voice_hard_blocks` is stale relative to PR #93/#95 (9 checks fail on `main`).
3. `test_booster_rewards` still fails on `main` for the `followup`/`edit_original_response` drift.
4. `/api/trade/history` raises `OperationalError` on a fresh database.
5. The dashboard hint for the voice-guard toggle says it affects Voice XP only, while PR #93 made it cover missions and MVP.
6. `test_afk`, `test_shop_emoji`, `test_shop_emoji_tls`, `test_shop_publications` fail on `main` (not diagnosed).
7. Optional follow-up: register `_role_owned_outside_level` in `RETENTION_READER_POLICY` (`utils/timed_ownership.py`); documentation only, no enforcing test, so not included.

## Files

| File | Action | Added | Removed | SHA-256 (first 16) | Reason |
|---|---|---:|---:|---|---|
| `cogs/leveling.py` | Modified | +27 | -0 | `de8ceded845dc411…` | Blacklist hooks in XP paths + blacklist_role_task sweep loop (the /rank 'no XP yet' guard is KEPT) |
| `cogs/reactionroles.py` | Modified | +472 | -116 | `59d2df54b325029c…` | Select-menu roles, duplicate-role buttons, custom_id routing (legacy rr_<role>_<msg> ids still resolve) |
| `cogs/welcome.py` | Modified | +15 | -5 | `70066d02fa4d457d…` | Rules-role assignment errors reported instead of raising |
| `dashboard/api/core.py` | Modified | +66 | -42 | `de60248b7f12ffbe…` | Role/channel pickers use shared validation and richer metadata (demo-only channel stub removed) |
| `dashboard/api/creator.py` | Modified | +21 | -2 | `574a2402f2fd450e…` | Role validation for Twitch give-role |
| `dashboard/api/embedbuilder.py` | Modified | +419 | -5 | `b563db2af0248157…` | Embed Studio send/persist with nonces, role validation for buttons and select options |
| `dashboard/api/leveling.py` | Modified | +159 | -20 | `2e822ec47a8418dd…` | Member-blacklist endpoints, role validation on reward and Prestige roles |
| `dashboard/api/moderation.py` | Modified | +24 | -8 | `c8c427a7b4fe3929…` | Role validation for warning-threshold role; inserts user_display_name; fixes edit-reason (query set a non-existent updated_at column) |
| `dashboard/api/mvp.py` | Modified | +14 | -1 | `3fb83a2d1a785212…` | Role validation for MVP role |
| `dashboard/api/tagmissions.py` | Modified | +14 | -0 | `5d26bef6df7027c7…` | Role validation for mission reward roles |
| `dashboard/app.py` | Modified | +292 | -42 | `b7cc1930e54fe9f2…` | Guild-context header check, server picker categories, POST select-guild with CSRF, safe next= redirect (demo-mode code NOT included) |
| `dashboard/auth.py` | Modified | +57 | -21 | `f4358243bc668611…` | Tri-state bot-membership check, Manage-Server discovery (demo-login bypass NOT included) |
| `dashboard/permissions.py` | Modified | +33 | -0 | `1562d798b01d01a5…` | ?g= guild-context conflict guard (demo-login and OWNER_ID additions NOT included) |
| `dashboard/static/css/command-manager.css` | Added | +620 | -0 | `fcc02c13043b4ee1…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/static/css/embed-composer.css` | Modified | +449 | -3 | `645b95ccf42f5394…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/static/css/main.css` | Modified | +644 | -47 | `f5ec00df844ae434…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/static/css/overview.css` | Added | +435 | -0 | `e8cba364e1ab51bb…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/static/css/shop-designer.css` | Modified | +2 | -0 | `0e5c61097e5f66ad…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/static/js/dashboard-tabs.js` | Added | +236 | -0 | `8df568235d638872…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/static/js/dashboard.js` | Modified | +622 | -36 | `01c6142ef6c6d6e0…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/static/js/embed-composer.js` | Modified | +614 | -19 | `d173fd2a3860cc2c…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/static/js/nero-select.js` | Modified | +96 | -42 | `24a57aa5328235f0…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/base.html` | Modified | +335 | -130 | `1b4c29a543f66176…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/config/access.html` | Modified | +13 | -3 | `435561cbb0b35ebf…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/config/announcements.html` | Modified | +4 | -4 | `972458ee6d08cf66…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/config/boost.html` | Modified | +23 | -11 | `5522d1f9f421531a…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/config/botprofile.html` | Modified | +13 | -11 | `c5f13b84aada25a9…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/config/creator.html` | Modified | +11 | -10 | `a2c27d14625663d2…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/config/general.html` | Modified | +27 | -17 | `5c54779f9f2914bd…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/config/welcome.html` | Modified | +34 | -22 | `0718f2220ccf0fa7…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/context_switch.html` | Added | +31 | -0 | `80e67054a7bfc8f7…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/general/auditlog.html` | Modified | +1 | -1 | `1830ae5629f4124b…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/general/backups.html` | Modified | +3 | -3 | `683209f04f16b243…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/general/health.html` | Modified | +24 | -22 | `d5828b962845840e…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/general/member_profile.html` | Modified | +9 | -9 | `59195a88dd85d536…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/general/members.html` | Modified | +38 | -8 | `8853c7571a636126…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/general/overview.html` | Modified | +196 | -47 | `5e4c1c76054c4e69…` | Overview redesign (status label keeps the original 'Bot Status' capitalisation) |
| `dashboard/templates/general/reports.html` | Modified | +22 | -4 | `650abd03f3029fcf…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/login.html` | Modified | +5 | -4 | `7ff7f25faff4500c…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/commands.html` | Modified | +220 | -376 | `13a8bcead1a0bbca…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/customcommands.html` | Modified | +2 | -2 | `58bcd78b27ba650e…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/embedbuilder.html` | Modified | +977 | -42 | `8da41d266cf1fe25…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/moderation.html` | Modified | +251 | -94 | `1bee955bed51c409…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/reactionroles.html` | Modified | +23 | -6 | `e654564265516d19…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/shopdesigner.html` | Modified | +2 | -2 | `40adaa95ffc871ac…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/shoppublisher.html` | Modified | +8 | -8 | `3194153a21b7dbac…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/tickets.html` | Modified | +21 | -12 | `f5f172001226ab31…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/manage/triggers.html` | Modified | +2 | -2 | `48c1f0c9f7d1cf38…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/server_select.html` | Modified | +203 | -150 | `d508130e2f71069e…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/economy.html` | Modified | +5 | -5 | `e9a58a48c2fd3757…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/events.html` | Modified | +1 | -1 | `144e587b8a25ce61…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/inventory.html` | Modified | +2 | -2 | `8789201db8d22958…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/ledger.html` | Modified | +2 | -2 | `c54c99d48ad0520b…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/leveling.html` | Modified | +155 | -33 | `0a0afcce48518424…` | Blacklist UI + truthful 'role cleanup deferred to the bot' messages + icon redesign |
| `dashboard/templates/systems/minigame_builder.html` | Modified | +48 | -9 | `f1125bcddfd0a479…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/minigames.html` | Modified | +5 | -5 | `f5bd5fd9a0840381…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/missions.html` | Modified | +13 | -13 | `89eabcd49b8586d6…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/mvp.html` | Modified | +26 | -11 | `cd713ff71b6dd8fa…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/shop.html` | Modified | +7 | -7 | `347e462ea61bcf88…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/tagmissions.html` | Modified | +18 | -16 | `ed6f50a14c5b6f65…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/tagpartners.html` | Modified | +18 | -16 | `09f24bca768a659f…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/templates/systems/trade.html` | Modified | +2 | -2 | `51293c0104d92b9c…` | Dashboard UI from the update (Studio / shell redesign / icons) |
| `dashboard/utils/role_validation.py` | Added | +233 | -0 | `5b4602678a837b9f…` | New: validates that a role is assignable by the bot before a dashboard save |
| `database.py` | Modified | +159 | -14 | `190c0e1bcf906c2d…` | reaction_roles rebuild (migrate_reaction_roles) + new tables: select menus, member blacklist restore queue, embed send nonces, member blacklist |
| `scripts/fixtures/nero_message_style_selectmenu_reference.json` | Added | +128 | -0 | `66278a476d44bdb3…` | Test/fixture from the update (supports the integrated features) |
| `scripts/preview_minigames_dashboard.py` | Modified | +1 | -0 | `e3bc2f27c2c8fa0e…` | Supporting script from the update |
| `scripts/test_blacklist_bot_sweep.py` | Added | +218 | -0 | `961882ffa2c4611a…` | New (ours): bot-side blacklist sweep, restore queue, API, cog wiring |
| `scripts/test_blacklist_shared_roles.py` | Added | +147 | -0 | `9371a47d71e7503a…` | New (ours): shared-role ownership regression tests |
| `scripts/test_booster_rewards.py` | Modified | +15 | -1 | `4099f57c02d4c3d9…` | Role-validation mock only (the loosened followup/footer assertion was NOT taken) |
| `scripts/test_channel_grouping_acceptance.py` | Added | +115 | -0 | `b2408d7d978b0124…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_dashboard_page_scripts.py` | Modified | +12 | -1 | `44ff10ace82aa1b3…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_embed_json_bridge.js` | Added | +375 | -0 | `3a5598d7d62238dc…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_leveling_reward_e2e.py` | Modified | +53 | -33 | `9bd0d364eaf9a65a…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_leveling_runtime_contract.py` | Modified | +15 | -2 | `1f799b9dbec516ef…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_member_blacklist_d3.py` | Added | +678 | -0 | `df3870bc45431c4c…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_moderation_edit_reason.py` | Added | +51 | -0 | `65d070a5784a34e2…` | New (ours): edit-reason regression test |
| `scripts/test_moderation_smooth_acceptance.py` | Added | +256 | -0 | `8c8d6008f9faeeba…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_moderation_smooth_navigation.js` | Added | +268 | -0 | `fc3736364e6487d4…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_nero_select_grouping.js` | Added | +217 | -0 | `7c9e6c10465afa41…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_phase2_backend.py` | Modified | +2 | -1 | `090dc09e392cdf46…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_reaction_roles_migration.py` | Added | +187 | -0 | `0a891da899b5af83…` | New (ours): reaction_roles rebuild safety tests |
| `scripts/test_select_roles_acceptance.py` | Added | +950 | -0 | `ae08bbcd154fd0c8…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_studio_acceptance.py` | Added | +1371 | -0 | `754c74a7cb118663…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_studio_select_ui_browser.js` | Added | +213 | -0 | `673010a9aa76565f…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_workstream1_acceptance.py` | Added | +316 | -0 | `26cb440edea4f699…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_workstream2_acceptance.py` | Added | +177 | -0 | `c1466e9f893a26d9…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_workstream2_roles_acceptance.py` | Added | +352 | -0 | `78504a7520ecb3ef…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_workstream3_tables_acceptance.py` | Added | +145 | -0 | `96050435184bcea6…` | Test/fixture from the update (supports the integrated features) |
| `scripts/test_workstream4_tabs_acceptance.py` | Added | +88 | -0 | `f4a2aa5147547f5d…` | ZIP test minus the demo-login deep-link test (feature excluded by decision) |
| `utils/level_claims.py` | Modified | +439 | -0 | `9342913d00345b45…` | Member Blacklist, shared-ownership guard (_role_owned_outside_level), bot-side sweep (process_blacklist_roles) |
| `utils/level_ops.py` | Modified | +5 | -0 | `256d77efc8917427…` | Blacklist enforcement for admin XP ops |
| `utils/permissions.py` | Modified | +80 | -6 | `7a1d4568198ae29a…` | check_bot_role_position now matches discord.py Role.is_assignable (+ Manage Roles check) |
| `utils/reward_engine.py` | Modified | +32 | -0 | `3d5c2d3f74db4a3a…` | Blacklist enforcement for rewards |
| `utils/xp_calculator.py` | Modified | +27 | -0 | `b82f247926edf1d8…` | Blacklist enforcement for XP |

### Full SHA-256 of every payload file

```
de8ceded845dc41140aa9f148ebd2a7c4faf0c6e9c00d9f3d14021b5ac43a044  cogs/leveling.py
59d2df54b325029ca0b5fb3e2abdae576837993a9e4d02e9c9ed0571b6b19eff  cogs/reactionroles.py
70066d02fa4d457daa7d6a57d20ff58139c24c108beffafd244ea0fcf79bea4a  cogs/welcome.py
de60248b7f12ffbe04b95a5c80d0e26d598627cbc95ce93696b1a7e58da2afbf  dashboard/api/core.py
574a2402f2fd450e86b6c7e8deb0f9d1e08ea5114132376ede1226f31427bad8  dashboard/api/creator.py
b563db2af0248157e8e2365ecb5d1569f5c70383bb64d14d5e01a6dab56d8005  dashboard/api/embedbuilder.py
2e822ec47a8418dd69524b7c35da2ac439aa0a3aff0ffa5b2ce1e380d2fb6e99  dashboard/api/leveling.py
c8c427a7b4fe39297e3fcee6f0e2536e1765f713dcc132f60af89c6f88f57fa1  dashboard/api/moderation.py
3fb83a2d1a785212ec8af58fefa1a715ff3264055007b6eebb6e567ddbe3da3e  dashboard/api/mvp.py
5d26bef6df7027c733eecf62d10624cdfb0e120a876b2ac69591c2571f8a928b  dashboard/api/tagmissions.py
b7cc1930e54fe9f2c699c08d2ffc08e292b3399e1fbe38d27853decb070709c8  dashboard/app.py
f4358243bc6686113f4b680b24e4d713ddc0759b69d71aa0dcc80f58e911c689  dashboard/auth.py
1562d798b01d01a5f16aafdedcd9427f3359a51f93c15a09e7f51d139ed6fce2  dashboard/permissions.py
fcc02c13043b4ee18fce6cdb371bc0dc19619454c9cb340f996faf56d79d4f36  dashboard/static/css/command-manager.css
645b95ccf42f5394962ea0b9bc5ced0423ebc53b2f4236230e2e3dc955dded02  dashboard/static/css/embed-composer.css
f5ec00df844ae434b8773648d3d2cd6b8945139b3fd6471da88dbffdfab91bc5  dashboard/static/css/main.css
e8cba364e1ab51bb5efda2beb45418178107f398f2331f78351a92bd10da46ad  dashboard/static/css/overview.css
0e5c61097e5f66ada1f99f027be7ea17ca1239959e008dcf745b0d7ea194eb21  dashboard/static/css/shop-designer.css
8df568235d6388727b798971a9dfbabf26640c6320060961d68ae7f5575eb2e9  dashboard/static/js/dashboard-tabs.js
01c6142ef6c6d6e0fb492559aecbbb64eedba63dd545555ab7903e6207b807fb  dashboard/static/js/dashboard.js
d173fd2a3860cc2c3417bc3bbaef7ad229ee6494584191cd2acbf4dba8e00b8e  dashboard/static/js/embed-composer.js
24a57aa5328235f0d46ecec8852bc08c29561b684050fc7ec9da914dc68caf09  dashboard/static/js/nero-select.js
1b4c29a543f66176a255ca01c1b8ae956e4cc5de9652442ee5114fdfcc850fed  dashboard/templates/base.html
435561cbb0b35ebfd36b6e370093d4ef9460eeb32b5aee30bc8be1ae608b51e1  dashboard/templates/config/access.html
972458ee6d08cf66143bc098118e7db386e587516898fcf5ba25d0ff926b3bef  dashboard/templates/config/announcements.html
5522d1f9f421531a058ebcfd3ca865fbe7f9fe340cebe935a43219fcaa87e0ed  dashboard/templates/config/boost.html
c5f13b84aada25a90b90fb92d234d634fc38dc14dbae5cd7f31d75c754aa993e  dashboard/templates/config/botprofile.html
a2c27d14625663d290e2b2586052ffd336c0afda95e7ca08735ef4564ff89716  dashboard/templates/config/creator.html
5c54779f9f2914bdbd31bb4bfb43b29be05cc7478bd1b711e511edf80e9baa24  dashboard/templates/config/general.html
0718f2220ccf0fa71c53e68142ec2e1eff676d751a5e8cf807732af8d50b1b21  dashboard/templates/config/welcome.html
80e67054a7bfc8f798f8b3fb0bd4cd7e4d9289d899b55302f22e3e368c40bcce  dashboard/templates/context_switch.html
1830ae5629f4124b71fa4aeaf871b3600fdab1ee600c1723789b81c7efbd1cef  dashboard/templates/general/auditlog.html
683209f04f16b24371937383fe7ac68efc25731882a125f1d06632075ba8f477  dashboard/templates/general/backups.html
d5828b962845840eeedafd9ab03729779acc3d877e21abddf5cfa315421a07e7  dashboard/templates/general/health.html
59195a88dd85d53631c7c5743fc4adf6ef90f703c02936721178493d986eb75f  dashboard/templates/general/member_profile.html
8853c7571a6361267a4507fa527dce446aed0cc658c9bf26b2a81ae68a087cb1  dashboard/templates/general/members.html
5e4c1c76054c4e69c3c54a61c4212b516497109434a5fbe19c5074828862bc5c  dashboard/templates/general/overview.html
650abd03f3029fcf70f3b1903b152efa01868a44e61e21c3249dbf27a93d7992  dashboard/templates/general/reports.html
7ff7f25faff4500cffa2500d06986448b9b42b41446f77c610b270ad9f09cd3a  dashboard/templates/login.html
13a8bcead1a0bbcaa692aff2a16864b851d2009a72d953d8079431b15224eb85  dashboard/templates/manage/commands.html
58bcd78b27ba650ec5bb866e9d4d0a73013d9dfc5a009918fb48cdc4b677cf67  dashboard/templates/manage/customcommands.html
8da41d266cf1fe25962dfd230829f97b29ac3bd1d6b903a8c48de243b0261a3a  dashboard/templates/manage/embedbuilder.html
1bee955bed51c4098a2e35530142128652e5cddcbee347a8d78eec8cedd1bab6  dashboard/templates/manage/moderation.html
e654564265516d19c4201c1c43398c136fba3117ae9f6928737c027201868c03  dashboard/templates/manage/reactionroles.html
40adaa95ffc871acfb4e15dd70daad2a149d2419de1706c8a3e533dcde4964bc  dashboard/templates/manage/shopdesigner.html
3194153a21b7dbace24e6f02f748328b82aae307aed12ad545a559ba2db81015  dashboard/templates/manage/shoppublisher.html
f5f172001226ab31052ff133ab420289bc8402481ba313e30ab4010e6e007c25  dashboard/templates/manage/tickets.html
48c1f0c9f7d1cf387856f420088ee516068939fb36052a7e2438dcaed8e9b4ea  dashboard/templates/manage/triggers.html
d508130e2f71069eef18a161cd2d6c002efaaf49f7f2c368ff03b68fc103cfdf  dashboard/templates/server_select.html
e9a58a48c2fd3757408bf4bd5761f4c93ffbb8372e96691a4d749e2e91e10cf7  dashboard/templates/systems/economy.html
144e587b8a25ce6172e0acb0ed1a6b15b0fd55b72f8643e9e9f2509fed7a2a17  dashboard/templates/systems/events.html
8789201db8d2295838d967e462b0c251dbf2eaa510e4f0359caf0826be1f1ffd  dashboard/templates/systems/inventory.html
c54c99d48ad0520b6bddef0e3df685acc1bcb419ada5b34166a0c56ad8540234  dashboard/templates/systems/ledger.html
0a0afcce48518424f6aa841cb56287c45d484fa20daebb3e08ed5f5e7e0ae892  dashboard/templates/systems/leveling.html
f1125bcddfd0a479f9d4064977bdc8a3ce1f39fb97158cd11b35ab4ec4b67f54  dashboard/templates/systems/minigame_builder.html
f5bd5fd9a0840381e50c68aaad650c9bbf9b9cf45511b5975dd14010e1f9664e  dashboard/templates/systems/minigames.html
89eabcd49b8586d67196e0d53d3ca5d0daa4cc98f7d9e3fd2bc6a82b7279c820  dashboard/templates/systems/missions.html
cd713ff71b6dd8fa8a5869518dcff4bbc917ee5256b91f45813aa098f6621b7a  dashboard/templates/systems/mvp.html
347e462ea61bcf88a26b601676b89f1b9c1ac7f59e405fbc67729100f153ad23  dashboard/templates/systems/shop.html
ed6f50a14c5b6f6522c25204391520033e33027ff59582ebc282d83bca228176  dashboard/templates/systems/tagmissions.html
09f24bca768a659fd2d70cb08bb92b518c594c7a7e810b470e14dab5912c4a58  dashboard/templates/systems/tagpartners.html
51293c0104d92b9c84f44f9c7046d4d7f6a58c1a58ee0bb811ec1402c306cff1  dashboard/templates/systems/trade.html
5b4602678a837b9fe219c41756571ac9203229ec81b5cc27ac56c66737631da8  dashboard/utils/role_validation.py
190c0e1bcf906c2df4b47b3cdf891618011bdd2196d5ab8e0349b61478f34450  database.py
66278a476d44bdb3f62eb234a2b4bdbdf5d9cfd6a892539f78ba04d89e83ac5c  scripts/fixtures/nero_message_style_selectmenu_reference.json
e3bc2f27c2c8fa0e98d7d7983370da22372cdeeaf08693ee24997ac13fee6d23  scripts/preview_minigames_dashboard.py
961882ffa2c4611affcf10c853ae882a181359ef994fcfff4216d144d6be16a1  scripts/test_blacklist_bot_sweep.py
9371a47d71e7503a270b04dc110a62ffae43c5ce2721d0b2b44dbe0cd4c815aa  scripts/test_blacklist_shared_roles.py
4099f57c02d4c3d9178c9866829837bc460931064aa300d560e04598e196d5fb  scripts/test_booster_rewards.py
b2408d7d978b0124b9fc08f65c33115e860407c41b6cc654ba64509d9d0a8a9e  scripts/test_channel_grouping_acceptance.py
44ff10ace82aa1b3966ca2e98027c4524852150a2220eca00215762e99c8e221  scripts/test_dashboard_page_scripts.py
3a5598d7d62238dc302fd39e28d1328d359a665de727b5099f9453045a26b62e  scripts/test_embed_json_bridge.js
9bd0d364eaf9a65a56556d3f5d9ed7e3f39eea3806ca38ed37df110ff7a86844  scripts/test_leveling_reward_e2e.py
1f799b9dbec516ef3ebd26e4dbd8ad90307f13b5d2e649e77ead757f2a16191b  scripts/test_leveling_runtime_contract.py
df3870bc45431c4c1582c2fecb4dc20d6fd0c870bdc634d0c57cde6ef3d54eed  scripts/test_member_blacklist_d3.py
65d070a5784a34e2e0a2dc4171b3bb279cac6b596c4850ccfa5a8fb89b82e04d  scripts/test_moderation_edit_reason.py
8c8d6008f9faeeba9ff155ab87144963baa545a64238ee0edd73e98d9a473734  scripts/test_moderation_smooth_acceptance.py
fc3736364e6487d4715c7738308140c8c8cc04a10e280b2114280b40c11c1151  scripts/test_moderation_smooth_navigation.js
7c9e6c10465afa41a17291955e745a3164575ac140178636e367095d9e0b4873  scripts/test_nero_select_grouping.js
090dc09e392cdf46a6605debfceccc095d285e5831b616434404c3220642ef24  scripts/test_phase2_backend.py
0a891da899b5af839a1bb3db545454df31e558fb8928218b4a1bb27e22c6e67d  scripts/test_reaction_roles_migration.py
ae08bbcd154fd0c86e020327c06e327215670307d50f7e67bf78d0b7b516d60a  scripts/test_select_roles_acceptance.py
754c74a7cb118663323581f19a7f8cb3b07dfcf4bc52d1c9b2c210c2548819fc  scripts/test_studio_acceptance.py
673010a9aa76565f2e9808c3614704ffbc2b85a1934a8299c77ad54330609077  scripts/test_studio_select_ui_browser.js
26cb440edea4f6999529124983af8158e7c8ab89da4c89ceeafd164b64b15ffc  scripts/test_workstream1_acceptance.py
c1466e9f893a26d9b177a48d5a2f69384fd242b87e8f4438625814ebccc21436  scripts/test_workstream2_acceptance.py
78504a7520ecb3efc2e63476c684d7d0566d0e30738844cfb5df4f69d27fe2f1  scripts/test_workstream2_roles_acceptance.py
96050435184bcea6df6f8a59689412ea99f12d779c305fd8414f6f4f77c9c336  scripts/test_workstream3_tables_acceptance.py
f4a2aa5147547f5df9a21b0daa29ed7077db21a4b5d6b100f3b6f8432aafb723  scripts/test_workstream4_tabs_acceptance.py
9342913d00345b456ad344af0aacee70932b1496a3b8b0de631839ad0bb0608c  utils/level_claims.py
256d77efc89174271cef92aa2d160102c8b12a219cc7e51d95825abf9e8e933b  utils/level_ops.py
7a1d4568198ae29a182a5d7d8b365a2aba121d5b31e899428ac562455fd46a07  utils/permissions.py
3d5c2d3f74db4a3a849cbd9a29e7e77384866b66ef8635f07a704ff96c1ddef0  utils/reward_engine.py
b82f247926edf1d85a9e7beb14377f5bfc67730ea8cffe81b18ffe283156ed95  utils/xp_calculator.py
```
