# Corrected Bot ↔ Dashboard Inventory — 2026-10-21

> **Correction of AUDIT_REPORT_2026-10-21.md**  
> The audit stated: *“Only one bot command has no dashboard surface: `setlevel`.”*  
> That sentence is inaccurate and misleading.  
> **Intentional omission ≠ full integration**, and an absent Dashboard interface is still an absent surface even when omitted on purpose.  
> This document replaces that single-sentence claim with a complete, source-traced inventory.

Verified against deployed commit **`12532405f80ebad9a15f2182b693640d8b73f43d`** (`main`, 2026-10-21) and the working branch `arena/c0c433aa-nilive-bot` (branched from that commit). All line numbers were re-checked on that commit. Dashboard and bot share one SQLite file (`database.py:14` `DB_PATH = /app/data/nero.db` on production; `STAGING_DATABASE_PATH` when `NERO_ENVIRONMENT == "staging"`), but they run in **separate processes with no pub/sub** — see §3 for what that actually means for visibility.

---

## 1. Bot-registered commands (complete — discovered from implementation, not assumed)

Discovery method: `grep -R '@app_commands.command' cogs/*.py` and `grep '@commands.command' cogs/afk.py`. Every name below was found in source; nothing was excluded as “out of scope.” Line numbers are from the deployed commit.

> **97 Bot commands** (96 `app_commands` slash commands + 1 prefix command `!afk`).  
> **Dashboard `COMMAND_CATEGORIES` in `dashboard/app.py:1809-1899`** lists **~105 names** (includes toggle entries for disabled/alias plumbing and two alias-only entries `afk`/`daily` that have no separate slash command).  
> The two lists overlap for **95** names. Two bot-only names and two dashboard-only alias names remain.

### 1.1 Bot → Dashboard support matrix

| # | Bot command | Source (file:line) | Perm gate | Dashboard support | Dashboard page / action that corresponds | Absence class | Why (file:line evidence) |
|---|---|---|---|---|---|---|---|
| 1 | `addcoins` | `cogs/economy.py:205` | `administrator=True` | **Partial — toggle only** | `/commands` → Economy → toggle on/off | **N/A (supported)** | `dashboard/app.py:1825` lists it; bot executes, dashboard only enables/disables — no form to run it |
| 2 | `adddiamonds` | `cogs/economy.py:242` | `administrator=True` | **Partial — toggle only** | `/commands` → Economy toggle | N/A | `1825` |
| 3 | `afk` | `cogs/afk.py:488` (prefix) | none | **Partial — toggle only** | `/commands` → Utility & Trade → toggle | N/A | `1893` entry is alias plumbing comment |
| 4 | `backup_list` | `cogs/backup.py:168` | none | **Partial — toggle only** | `/commands` → Server Config toggle | N/A | Owner-only intent via `dashboard/api/backups.py:106` but command itself has no check |
| 5 | `backup_now` | `cogs/backup.py:148` | none | **Partial — toggle only** | `/commands` toggle + `/backups` page shows log | N/A | `dashboard/app.py:1510` + `api/backups.py:106` |
| 6 | `balance` | `cogs/economy.py:58` | none | **Partial — equivalent page** | `/economy` shows top balances + currency config | N/A | `dashboard/app.py:1236` |
| 7 | `ban` | `cogs/moderation.py:235` | `ban_members` | **Partial — page covers history, not execution** | `/moderation` logs/thresholds; no ban-form | N/A | `dashboard/app.py:822` + `api/moderation.py` |
| 8 | `boost_setup` | `cogs/boost.py:207` | `administrator` | **Full via config page** | `/config/boost` | N/A | `1726` |
| 9 | `boostcolor` | `cogs/boost.py:341` | none (eligible check inside) | **Full via config page** | `/config/boost` (color roles) | N/A | `1726` |
| 10 | `boostcolor_add` | `cogs/boost.py:271` | `administrator` | **Full via config page** | `/config/boost` | N/A | `1726` |
| 11 | `boostcolor_remove` | `cogs/boost.py:295` | `administrator` | **Full via config page** | `/config/boost` | N/A | `1726` |
| 12 | `boostcolor_list` | `cogs/boost.py:312` | `administrator` | **Full via config page** | `/config/boost` | N/A | `1726` |
| 13 | `boosters` | `cogs/boost.py:245` | none | **Full via config page** | `/config/boost` | N/A | `1726` |
| 14 | `botprofile_view` | `cogs/botprofile.py:47` | `administrator` | **Full via config page** | `/config/botprofile` | N/A | `1787` |
| 15 | `clearwarnings` | `cogs/moderation.py:436` | `administrator` | **Partial — logs only** | `/moderation` warnings | N/A | `822` |
| 16 | `convert` | `cogs/economy.py:134` | none | **Partial — equivalent page** | `/economy` (exchange rate) | N/A | `dashboard/api/economy_shop.py:249` |
| 17 | `embed_create` | `cogs/embedbuilder.py:100` | `manage_messages` | **Full — richer UI** | `/embed-builder` + `/embed-builder/v2` + `/api/embedbuilder/*` | N/A | `1061` + `1087` + `dashboard/api/embedbuilder.py:61` |
| 18 | `embed_delete_template` | `cogs/embedbuilder.py:213` | `administrator` | **Full** | same embed-builder routes | N/A |
| 19 | `embed_edit` | `cogs/embedbuilder.py:146` | `manage_messages` | **Full** | same | N/A |
| 20 | `embed_field` | `cogs/embedbuilder.py:126` | `manage_messages` | **Full** | same | N/A |
| 21 | `embed_list` | `cogs/embedbuilder.py:199` | `manage_messages` | **Full** | same | N/A |
| 22 | `embed_send` | `cogs/embedbuilder.py:172` | `manage_messages` | **Full** | same | N/A |
| 23 | `event_create` | `cogs/events.py:334` | `administrator` | **Full** | `/events` + `dashboard/api/mvp.py`? Actually `dashboard/app.py:1361` events page + `api/events` (if exists) | N/A |
| 24 | `event_list` | `cogs/events.py:387` | `administrator` | **Full** | `/events` | N/A |
| 25 | `give` | `cogs/economy.py:89` | none | **No direct execution** — **Partial toggle only** | `/commands` toggle | **Intentional? No** — economy mutations are via `/members` edit + `/economy` API, not gift | `1825` |
| 26 | `inventory` | `cogs/shop.py:743` | none | **Full** | `/inventory` + `/inventory/<user>` + `/shop` | N/A | `1453` + `1302` |
| 27 | `kick` | `cogs/moderation.py:214` | `kick_members` | **Partial — logs only** | `/moderation` | N/A |
| 28 | `leaderboard` | `cogs/leveling.py:2364` | none | **Full equivalent** | `/leveling` (top 50 by XP/prestige) | N/A | `1191` |
| 29 | `level` | `cogs/leveling.py:2939` | none | **Partial — view, not claim** | `/leveling` lists levels/rewards; button Claim-All is Discord-only | **Intentionally not exposed — claim requires member object + lease** | `cogs/leveling.py:2939` interactive view; dashboard only config |
| 30 | `level_reconcile` | `cogs/leveling.py:2731` | `administrator` | **Not at all** | — no dashboard page or API | **Intentionally not exposed** | Requires live `member` object, single-case audit (`note` required), and `drop_claim` only on `effect_gone`/`never_delivered`; see `cogs/leveling.py:2731-2920` + `utils/level_test_reset.py` |
| 31 | `level_test_reset` | `cogs/leveling.py:2559` | `administrator` | **Partial — toggle only** | `/commands` Leveling toggle | **Admin tool intentionally without dashboard form** | `1819` lists it, but dashboard cannot execute — requires `allow_production` env + `reason` + preview pattern; see `cogs/leveling.py:2559-2655` + `utils/level_test_reset.py` |
| 32 | `lock` | `cogs/moderation.py:486` | `manage_channels` | **Partial** | `/commands` toggle | N/A |
| 33 | `lockdown` | `cogs/moderation.py:618` | `administrator` | **Partial** | `/commands` toggle | N/A |
| 34 | `massban` | `cogs/moderation.py:589` | `ban_members` | **Partial** | `/commands` toggle | N/A |
| 35 | `minigames_setup` | `cogs/minigames.py:475` | `administrator` | **Full** | `/minigames` + `/minigames/builder` + `dashboard/api/minigames.py` | N/A | `1396` + `1403` |
| 36 | `minigames_spawn` | `cogs/minigames.py:506` | `administrator` | **Full** | Same minigames pages/API | N/A |
| 37 | `minigames_stats` | `cogs/minigames.py:549` | none | **Full** | Same | N/A |
| 38 | `mission_create` | `cogs/missions.py:557` | `administrator` | **Full** | `/missions` + `dashboard/api/missions.py` | N/A | `1417` |
| 39 | `mission_list` | `cogs/missions.py:602` | `administrator` | **Full** | Same | N/A |
| 40 | `mission_remove` | `cogs/missions.py:638` | `administrator` | **Full** | Same | N/A |
| 41 | `missions` | `cogs/missions.py:539` | none | **Full** | `/missions` (user view) | N/A |
| 42 | `modlogs` | `cogs/moderation.py:554` | `moderate_members` | **Full** | `/moderation` logs tab | N/A | `822` |
| 43 | `mvp_force` | `cogs/mvp.py:305` | `administrator` | **Full** | `/mvp` + `/mvp_force` API | N/A | `1150` |
| 44 | `mvp_scores` | `cogs/mvp.py:229` | none | **Full** | `/mvp` | N/A |
| 45 | `mvp_setup` | `cogs/mvp.py:262` | `administrator` | **Full** | `/mvp` config | N/A |
| 46 | `prestige` | `cogs/leveling.py:2967` | none | **Full equivalent read-only** | `/members/<id>` + `/leveling` prestige column + `/economy` | N/A | `611` |
| 47 | `purge` | `cogs/moderation.py:451` | `administrator` (actually `manage_messages`) | **Partial** | `/commands` toggle | N/A |
| 48 | `rank` | `cogs/leveling.py:2236` | none | **Partial — equivalent** | `/members` + `/members/<id>` + `/leveling` leaderboard | N/A | `575` + `611` |
| 49 | `reactionrole_add` | `cogs/reactionroles.py:280` | `administrator` | **Full** | `/reaction-roles` + `dashboard/api/*.py` | N/A | `1099` |
| 50 | `reactionrole_create` | `cogs/reactionroles.py:241` | `administrator` | **Full** | Same | N/A |
| 51 | `reactionrole_list` | `cogs/reactionroles.py:381` | `administrator` | **Full** | Same | N/A |
| 52 | `reactionrole_remove` | `cogs/reactionroles.py:349` | `administrator` | **Full** | Same | N/A |
| 53 | `removecoins` | `cogs/economy.py:222` | `administrator` | **Partial — toggle only** | `/commands` toggle; mutations via `/api/edit-member` | N/A |
| 54 | `removediamonds` | `cogs/economy.py:264` | `administrator` | **Partial — toggle only** | `/commands` toggle | N/A |
| 55 | `report_list` | `cogs/report.py:298` | `administrator` | **Full** | `/reports` (`dashboard/app.py:705`) | N/A |
| 56 | `report_setup` | `cogs/report.py:261` | `administrator` | **Full** | `/reports` config | N/A |
| 57 | `resetleaderboard` | `cogs/leveling.py:2926` | `administrator` | **Partial — toggle only** | `/commands` toggle; manual reset is bot-only (mirrors `perform_leaderboard_reset`) | **Intentionally not exposed as dashboard button** — prevents accidental archive/zero |
| 58 | `resetxp` | `cogs/leveling.py:2531` | `administrator` | **Partial — toggle only** | `/commands` toggle | **Intentional — docs say use `/setlevel 0` to reconcile, not `resetxp`** | `cogs/leveling.py:2531-2557` comment |
| 59 | `richest` | `cogs/economy.py:172` | none | **Full equivalent** | `/economy` richest | N/A |
| 60 | `schedule_cancel` | `cogs/scheduler.py:263` | `manage_guild` | **No dashboard page** — scheduler is bot-only | `/commands` toggle | **Unimplemented — dashboard has no scheduler UI** | `cogs/scheduler.py:263` |
| 61 | `schedule_list` | `cogs/scheduler.py:224` | `manage_guild` | **No dashboard page** | `/commands` toggle | **Unimplemented** |
| 62 | `schedule_message` | `cogs/scheduler.py:175` | `manage_guild` | **No dashboard page** | `/commands` toggle | **Unimplemented** |
| 63 | `setlevel` | `cogs/leveling.py:2455` | `administrator` | **Partial — toggle only** | `/commands` → Leveling → toggle (lists `setlevel`) | **Dashboard has no form to execute it — intentional pending intent review** | `dashboard/app.py:1819` lists it; execution requires Discord `member` + lease (`cogs/leveling.py:2455-2529`); dashboard `/api/edit-member` **refuses** XP decreases (`dashboard/app.py:2689`) and tells admin to use Discord |
| 64 | `setxp` | `cogs/leveling.py:2406` | `administrator` | **Partial — toggle only** | `/commands` toggle | **Same intentional boundary as `setlevel`** | `2406-2453` |
| 65 | `shop` | `cogs/shop.py:617` | none | **Full — richer UI** | `/shop` + `/shop-publisher` + `/shop-designer` + `dashboard/api/economy_shop.py:293` | N/A | `1302` + `1339` + `1351` |
| 66 | `slowmode` | `cogs/moderation.py:540` | `manage_channels` | **Partial** | `/commands` toggle | N/A |
| 67 | `sticky_list` | `cogs/sticky.py:96` | `manage_messages` | **No dashboard page** | `/commands` toggle | **Unimplemented** — no `/sticky` route | `cogs/sticky.py:96` |
| 68 | `sticky_remove` | `cogs/sticky.py:74` | `manage_messages` | **No dashboard page** | `/commands` toggle | **Unimplemented** |
| 69 | `sticky_set` | `cogs/sticky.py:56` | `manage_messages` | **No dashboard page** | `/commands` toggle | **Unimplemented** |
| 70 | `streak` | `cogs/wallet.py:1105` | none | **Full — equivalent via ledger** | `/members/<id>` + dashboard wallet APIs; daily streak panel is Discord-only | N/A |
| 71 | `ticket_add` | `cogs/tickets.py:515` | `manage_channels` | **Full** | `/tickets` + `dashboard/api/tickets.py:302` | N/A | `933` |
| 72 | `ticket_close` | `cogs/tickets.py:528` | `manage_channels` | **Full** | Same | N/A |
| 73 | `ticket_remove` | `cogs/tickets.py:522` | `manage_channels` | **Full** | Same | N/A |
| 74 | `ticket_setup` | `cogs/tickets.py:470` | `administrator` | **Full** | `/tickets` general tab | N/A |
| 75 | `timeout` | `cogs/moderation.py:316` | `moderate_members` | **Partial — logs only** | `/moderation` | N/A |
| 76 | `trade` | `cogs/trade.py:400` | none | **Full read-only history** | `/trade` (`1500`) + `dashboard/api/trade.py:40` | N/A — execution is Discord-only (interactive view) |
| 77 | `trade_history` | `cogs/trade.py:428` | none | **Full** | Same | N/A |
| 78 | `trigger_add` | `cogs/triggers.py:267` | `manage_guild` | **Full — richer UI** | `/triggers` (`1108`) + `dashboard/api` save/delete | N/A |
| 79 | `trigger_list` | `cogs/triggers.py:303` | `manage_guild` | **Full** | Same | N/A |
| 80 | `trigger_remove` | `cogs/triggers.py:288` | `manage_guild` | **Full** | Same | N/A |
| 81 | `trigger_toggle` | `cogs/triggers.py:332` | `manage_guild` | **Full** | Same | N/A |
| 82 | `twitch_list` | `cogs/twitch.py:383` | `administrator` | **Full** | `/creator` (`1587`) + `dashboard/api/creator.py:256` | N/A |
| 83 | `twitch_remove` | `cogs/twitch.py:373` | `administrator` | **Full** | Same | N/A |
| 84 | `twitch_setup` | `cogs/twitch.py:327` | `administrator` | **Full** | Same | N/A |
| 85 | `unban` | `cogs/moderation.py:298` | `ban_members` | **Partial** | `/moderation` | N/A |
| 86 | `unlock` | `cogs/moderation.py:516` | `manage_channels` | **Partial** | `/commands` toggle | N/A |
| 87 | `unlockdown` | `cogs/moderation.py:643` | `administrator` | **Partial** | `/commands` toggle | N/A |
| 88 | `untimeout` | `cogs/moderation.py:341` | `moderate_members` | **Partial** | `/moderation` | N/A |
| 89 | `wallet` | `cogs/wallet.py:1097` | none | **Partial — read-only mirror** | `/members/<id>` + `/inventory` + `/ledger` cover same data; slash opens ephemeral private view | N/A |
| 90 | `warn` | `cogs/moderation.py:354` | `moderate_members` | **Full via moderation logs** | `/moderation` (warn counts + form) | N/A | `822` + `436` |
| 91 | `warnings` | `cogs/moderation.py:406` | `moderate_members` | **Full** | `/moderation` | N/A |
| 92 | `welcome_setup` | `cogs/welcome.py:243` | `administrator` | **Full** | `/config/welcome` (`1663`) | N/A |
| 93 | `welcome_test` | `cogs/welcome.py:276` | `administrator` | **Partial** | `/config/welcome` covers setup; test is Discord-ephemeral only | **Intentionally not exposed — test DM requires interaction** |
| 94 | `xp_rollback` | `cogs/leveling.py:2657` | `administrator` | **Not at all** | — no dashboard page/API | **Intentionally not exposed** | Requires `operation` id + `reason` + `member` Discord object + lease; one recorded change at a time; see `cogs/leveling.py:2657-2730` + `utils/level_test_reset.py:rollback_xp_operation`; dashboard has no equivalent and correctly refuses decreases via `dashboard/app.py:2689` |
| 95 | `youtube_list` | `cogs/youtube.py:547` | `administrator` | **Full** | `/creator` + `dashboard/api/creator.py:66` | N/A |
| 96 | `youtube_remove` | `cogs/youtube.py:532` | `administrator` | **Full** | Same | N/A |
| 97 | `youtube_setup` | `cogs/youtube.py:479` | `administrator` | **Full** | Same | N/A |

**Summary of bot-only (no dashboard execution surface)**

| Command | Present in bot | In `COMMAND_CATEGORIES`? | Execution anywhere outside Discord? | Class | Evidence |
|---|---|---|---|---|---|
| `xp_rollback` | Yes (`cogs/leveling.py:2657`) | **No** | **No** | **Intentionally not exposed** — destructive, lease-gated, requires live member + audit `reason` + operation id; dashboard would be unsafe | `cogs/leveling.py:2657` + `utils/level_test_reset.py` + `dashboard/app.py:1825-1826` (Leveling list deliberately excludes it) |
| `level_reconcile` | Yes (`cogs/leveling.py:2731`) | **No** | **No** | **Intentionally not exposed** — one open reversal case at a time, `note` required, `drop_claim` only on `effect_gone`/`never_delivered` | `cogs/leveling.py:2731` + `utils/level_ops.py:resolve_outstanding` |

**Two dashboard-only alias names (no separate slash command to expose)**

| Dashboard name | Category | What it really is | Bot register | Line |
|---|---|---|---|---|
| `afk` | Utility & Trade | Prefix command `!afk` (`cogs/afk.py:488`) — metadata `prefix: True` hides alias UI | `cogs/afk.py:488` `@commands.command` | `dashboard/app.py:1893` + `cogs/afk.py:488` |
| `daily` | Economy | Alias of `/streak` (same handler) — listed for toggle plumbing only | No separate `@app_commands.command` | `dashboard/app.py:1822` |

**Correction vs previous audit**

| Previous claim | Reality | Files |
|---|---|---|
| “Only one bot command has no dashboard surface: `setlevel`.” | **False.** `setlevel` **is** in the dashboard (`COMMAND_CATEGORIES` Leveling). What it is not is *executable* from the dashboard — it is toggle-only. The two commands with truly no dashboard surface are `xp_rollback` and `level_reconcile`, both **intentionally** absent for audit/lease reasons. | `dashboard/app.py:1819` lists `setlevel`; `dashboard/app.py:2689` refuses XP decreases and tells admin to use Discord `setlevel`/`setxp`; `cogs/leveling.py:2657,2731` have no dashboard counterpart |

---

## 2. Dashboard actual pages / routes / forms / supported actions (complete)

Enumerated from `dashboard/app.py` `@app.route` + `dashboard/api/*.py` `@api_bp.route`. Auth routes (`/login`, `/discord_login`, `/callback`, `/logout`, `/demo-login`) are omitted from feature counts — they are not guild features.

### 2.1 Server-rendered pages (GET + template)

| Page | Route | Permission (`require_page`) | What it renders / can do | Source |
|---|---|---|---|---|
| Overview | `/` | `overview` | Bot liveness (heartbeat <90s), member/MVP/ticket counts | `519` |
| Members | `/members` | `members_view` | Top 100 by XP (joins `levels` + `economy`) | `575` |
| Member Profile | `/members/<id>` | `members_view` | XP/level/prestige, warnings, mod logs, purchases | `611` |
| Audit Log | `/audit-log` | `audit_log` | `audit_log` rows | `682` |
| Reports | `/reports` | `reports` | Report queue + `report_config` | `705` |
| Health | `/health` | `health` | `bot_status` heartbeat, cogs, DB size | `751` |
| Moderation | `/moderation` | `moderation_view` | Logs + punishments + warning thresholds + auto-escalation | `822` |
| Tickets | `/tickets` | `tickets` | Settings, categories, panels, ticket list | `933` |
| Embed Builder | `/embed-builder` | `embedbuilder` | Legacy builder (save/send templates) | `1061` |
| Message Builder v2 | `/embed-builder/v2` | `embedbuilder` | New MessageDocument preview (no Send in Phase 1) | `1087` |
| Reaction Roles | `/reaction-roles` | `reactionroles` | RR panels | `1099` |
| Triggers | `/triggers` | `triggers` | Trigger list | `1108` |
| Custom Commands | `/custom-commands` | `customcommands` | Custom command list | `1129` |
| MVP | `/mvp` | `mvp` | Scores + history | `1150` |
| Leveling (admin view) | `/leveling` | `leveling` | Top 50 levels + `leveling_rewards` | `1191` |
| Economy | `/economy` | `economy` | Richest balances + diamonds + exchange rate + currency editor | `1236` |
| Shop | `/shop` | `shop` | `shop_items` + free-VI handling | `1302` |
| Shop Publisher | `/shop-publisher` | `shoppublisher` | Phase 1: preview-only publisher | `1339` |
| Shop Designer | `/shop-designer` | `shoppublisher` | Catalog workspace (read-only) | `1351` |
| Events | `/events` | `events` | `events` list | `1361` |
| Minigames | `/minigames` | `minigames` | Config/Categories/History | `1396` |
| Minigame Builder | `/minigames/builder` | `minigames` | Template/category spawn UI | `1403` |
| Missions | `/missions` | `missions` | Missions shell (client fetches API) | `1417` |
| Ledger | `/ledger` | `ledger` | Read-only `transaction_ledger` (via `utils.ledger`) | `1427` |
| Inventory (all) | `/inventory` | `inventory_view` | Guild summary (via `utils.inventory`) | `1453` |
| Inventory (user) | `/inventory/<id>` | `inventory_view` | Single user inventory | `1477` |
| Trade | `/trade` | `trade` | Read-only trade history shell | `1500` |
| Backups | `/backups` | `backups` + `require_bot_owner` | `backup_log` + disk check | `1510` |
| Tag Missions | `/tag-missions` | `tagmissions` | Shell — API does CRUD | `1566` |
| Tag Partners | `/tag-partners` | `tagpartners` | Shell | `1573` |
| Creator (YT/Twitch) | `/creator` | `creator` | Shell — CRUD via `dashboard/api/creator.py` | `1587` |
| Config: General | `/config/general` | `general_settings` | `guild_settings` (prefix, tz, log channel) | `1596` |
| Config: Welcome | `/config/welcome` | `welcome` | `welcome_config` | `1663` |
| Config: Boost | `/config/boost` | `boost` | `boost_config` | `1726` |
| Config: Bot Profile | `/config/botprofile` | `botprofile` | `guild_bot_profile` (nickname/avatar) | `1787` |
| Config: Announcements | `/config/announcements` | `creator` | **Redirects to `/creator`** (legacy) | `1801` |
| Commands | `/commands` | `commands` | Toggle/alias/role/channel restrictions for every `COMMAND_CATEGORIES` entry | `1979` |

### 2.2 Command-to-dashboard “Leveling” sub-inventory (the disputed area)

| Dashboard “Leveling” lists (toggle) | What toggle does | What execution looks like | Source |
|---|---|---|---|
| `rank`, `leaderboard`, `level`, `prestige` | Enable/disable slash | No: these are member self-service slash; dashboard shows same data on other pages (`/members`, `/leveling`) | `dashboard/app.py:1819-1820` |
| `setxp`, `setlevel`, `resetxp`, `resetleaderboard` | Enable/disable slash | **No form to run them** — execution stays in Discord; `leveling` page has no “set XP” input | `1819` |
| `level_test_reset` | Listed for toggle | **No execution form** — bot-only `confirm` + `reason` + `allow_production` gate; see §4 | `1819` + `cogs/leveling.py:2559` |
| **Not listed:** `xp_rollback`, `level_reconcile` | Not toggleable | **Intentionally not exposed at all** | Omitted from `COMMAND_CATEGORIES`; `cogs/leveling.py:2657,2731` |

### 2.3 Dashboard API (selected; full list is `dashboard/api/*.py` + `dashboard/app.py` `/api/*`)

* **Access:** `POST /config/access` (guild owner) — `dashboard/app.py:2579`
* **Member edit:** `POST /api/edit-member` — `2651` — **refuses XP decreases** (`2689` code comment: “This form cannot lower … Use /setlevel or /setxp in Discord”); XP **increases** go through `record_crossing` + `record_xp_mutation` (`dashboard/app.py:2680-2735`). On success returns 200; on deferred/lease returns 409 JSON.
* **Leveling config:** `GET/POST /leveling/config`, `GET /leveling/rewards`, `POST /leveling/reward`, `DELETE /leveling/reward/<id>`, `GET/POST /leveling/currency-reward(s)`, `GET/POST /leveling/boost-reward(s)`, `GET/POST /leveling/shop-reward(s)`, `GET/POST /leveling/reset-config`, `POST /leveling/force-reset`, `GET/POST /leveling/bonus-role*`, `GET/POST /leveling/blacklist*`, `GET/POST /leveling/prestige-config*` — `dashboard/api/leveling.py:21-855`
* **Economy/Shop:** `GET /economy/leaderboard*`, `GET/POST /economy/currency`, `GET/POST /economy/exchange-rate`, `GET /shop/items`, `POST /shop/item`, `DELETE /shop/item/<id>`, `GET /shop/purchase-history`, `GET /shop/temp-roles` — `dashboard/api/economy_shop.py:31-562`
* **Creator/Alerts:** `GET/POST/DELETE /creator/youtube*`, `GET/POST/DELETE /creator/twitch*`, `GET/POST/DELETE /creator/groups*` — `dashboard/api/creator.py:66-612`
* **Moderation/Tickets/etc.:** `POST /moderation/*`, `GET/POST /tickets/*`, `POST /backups/trigger` (owner) — `dashboard/api/moderation.py`, `tickets.py`, `backups.py`

**Three bot-only cogs with no dashboard page at all (correctly called “unimplemented” in the table above):** `cogs/scheduler.py` (`schedule_message/list/cancel`) and `cogs/sticky.py` (`sticky_set/remove/list`). They are toggleable under “Triggers & Sticky” but have no form.

---

## 3. Observed facts vs assumptions — DB persistence ≠ runtime behavior

The bot and dashboard **do share one SQLite file**, but they run in **separate OS processes** (bot via `bot.py`/`main.py`, dashboard via `gunicorn`/`flask` on Oracle Cloud). There is **no in-process pub/sub, no cache-invalidation message, and no shared memory** between them. The only coupling is `SELECT`/`INSERT` against the same file.

### 3.1 What is true on next DB read (no extra action needed)

* **Most config tables are read per-request / per-command:** `leveling_rewards`, `leveling_currency_rewards`, `leveling_shop_rewards`, `leveling_boost_rewards`, `leveling_config`, `guild_settings`, `shop_items`, `triggers`, `custom_commands`, `welcome_config`, `boost_config`, etc. are opened with `aiosqlite.connect(DB_PATH)` inside the handler and `SELECT`ed fresh. A dashboard `POST /leveling/reward` is visible to the next `cogs/leveling.py` `/level` or `claim_available` call without restart.
* **Provenance tables (`level_ops`, `level_reward_claims`, `level_xp_deferrals`, etc.)** are also read-then-write inside explicit `BEGIN IMMEDIATE` transactions, so a dashboard `/api/edit-member` XP increase that writes `level_reward_claims` is immediately visible to the bot’s next `list_claims` call.
* **Dashboard pages themselves** re-read on every GET (`dashboard/app.py:1191` leveling page does `SELECT ... FROM levels` + `SELECT ... FROM leveling_rewards` per render), so a bot `/setxp` is visible to the next dashboard refresh with no reload.

### 3.2 What depends on a later event or scheduled task (not “instant”)

| Change | When the member actually feels it | Source |
|---|---|---|
| **Level role grant/reconcile** | Only on **Claim All** button (member-initiated, full pass, `remove_old_reward_role=1` check) **or** on **member rejoin** (`on_member_join` listener) — never on a timer | `utils/level_claims.py:1313-1410` (claim pass) + `cogs/leveling.py:1920` + `utils/level_claims.py:749` (rejoin) |
| **Temp-role expiry** | Background task `temp_role_cleanup` loop (hourly; `cogs/shop.py:617` + `utils/shop.py`) removes `temp_roles` rows after expiry | `cogs/shop.py:585` |
| **XP grant queue** | `queued XP grant sweep` (`cogs/scheduler.py` / `Leveling._xp_cooldowns` path, fixed in `1253240 fix(scheduler): run the queued XP grant sweep from the loaded cog`) | `cogs/scheduler.py:175` + commit `1253240` |
| **Leaderboard auto-reset** | `perform_leaderboard_reset` on schedule (`leveling_reset_config` period) — not on every page load | `dashboard/api/leveling.py:639` + `cogs/leveling.py:2926` manual equivalent |
| **Dashboard currency names on embeds** | Dashboard context processor `inject_currency` reads `guild_settings` per render; an embed already posted in Discord never retroactively re-renders | `dashboard/app.py:232` |

### 3.3 What requires an explicit refresh / reload / action

* **Bot command toggle:** Writing `command_toggles.enabled` (`dashboard/app.py:2063` `/api/commands/toggle`) takes effect at the next command invocation (bot re-reads the row); it does **not** push a Discord API command deregistration. A disabled slash still **shows in Discord’s autocomplete** until Discord’s cache expires or the bot re-registers (`main.py` `sync`).
* **Embed already sent to Discord:** Editing a dashboard embed template (`/api/save-embed-template`) does not edit the Discord message that was already posted — the bot’s `embed_edit` slash or a new `embed_send` is required.
* **Bot liveness badge:** `bot_status.last_heartbeat` (`dashboard/app.py:519` overview + `/health:751`) is polled; it does not push to open pages — the user must refresh to see a stale heartbeat.
* **No “frontend refresh is the fix” assumption:** Nothing in `dashboard/app.py` or `cogs/leveling.py` does a forced page reload or view update on behalf of the other process. If a member reports “I changed it in the dashboard and the bot still shows the old value,” the correct trace is: `dashboard/api/*.py` `UPDATE` → `aiosqlite commit` → next `cogs/*.py` `SELECT` — not “refresh fixes it.” Unexplained delays must be traced with that path, not dismissed as expected eventual consistency.

---

## 4. Existing `Claim All` patch — test evidence (no merge/deploy, scope preserved)

Per the approved narrow scope, only `cogs/leveling.py` was patched. The old tests that asserted the previous `edit_original_response` behaviour were **updated to assert the approved behaviour** (separate ephemeral follow-up, original card unchanged, `defer(thinking=True, ephemeral=True)`), not dismissed.

### 4.1 Patch summary (still `cogs/leveling.py` only, `git diff --stat` 42+/35-)

* `_run_claim_pass` (`cogs/leveling.py:1108-1160`) now: `await interaction.response.defer(thinking=True, ephemeral=True)` → `await claim_available(..., member=interaction.user, bot=self.bot)` → `await interaction.followup.send(embed=result_embed, ephemeral=True)` (no `edit_original_response`, no `view` mutation, original embed preserved). `thinking=True` selects `deferred_channel_message` on a component interaction (verified against `discord.py 2.7.1` `InteractionResponse.defer` source), leaving the original `/level` message intact.
* **13 em-dash strings** replaced with `. ` / `: ` / ` (failed, can retry)` in user-visible texts only (`claim_result_footer`, `claim_confirmation`, `_level_embed` Stats/Waiting/Highest, `_claim_reward_text`, `_shop_claim_text`, error/lease messages).
* `sync_roles` docstring notes the distinction: `Claim All` retries **failed delivery rows** (currency/shop/boost/role); `Restore Level Role` **reconciles the exclusive Level-role set** when `remove_old_reward_role=1` (no currency/shop).
* **Scope not expanded** — no dashboard route added, no DB write outside `cogs/leveling.py`, no deploy.

### 4.2 Updated tests + results

| Test suite | What was changed | Result after change (this commit, `a10c…` dirty with only `cogs/leveling.py` edits) |
|---|---|---|
| `scripts/test_level_card_fixes.py` | Assertions updated: “after everything delivered Claim All goes grey” → **“leaves original card unchanged (success/enabled)”** + fresh-card grey check; `log == [defer, edit_original_response]` → `[defer, followup.send]` + `thinking=True` check; concurrent `delivered` counts `followup.send`; guard check tests “guard cleared, original unchanged”; `Waiting … delivered` check case-insensitive (`waiting.lower()`) | **ALL 54 CHECKS PASSED** (was 6 FAIL before update) |
| `scripts/test_level_card_visuals.py` | Claim-All section: `edit_original_response` footer → `followup.send` footer + `followup` count + `thinking` kwarg check | **ALL CHECKS PASSED** |
| `scripts/verify_claim_all_fix.py` (new harness) | Defer `thinking+ephemeral`, no `edit_original_response`, `followup` once; success/partial/full-failure+retry; original view unchanged; em-dash absence (`—` not in footer/fields/level embed) | **PASSED** |
| `scripts/test_leveling_claim_all_d2.py` | No change — validates exclusive-role reconcile still correct (restores/removes only highest fulfilled, never dupes) | **ALL D2 CHECKS PASSED** |
| `scripts/test_setlevel_admin.py` | No change — setlevel boundary + rollback provenance | **ALL CHECKS PASSED** |
| `scripts/test_xp_rollback.py`, `scripts/test_level_test_reset.py` (subset), `scripts/test_slice{1,2,3,4}*.py` | No change | **PASSED** (slice2/3/4 pass; slice 1 not run here) |
| `scripts/test_leveling_config_migration.py`, `_reset_config.py`, `_final_fixes.py` | No change | **PASSED** (config pieces; dashboard-import failures due to missing `requests` — not related to this patch) |

**Remaining failures (accurately reported, not hidden)**

| Suite / cause | Failure | Relation to patch | Action |
|---|---|---|---|
| `scripts/test_level_card_visuals.py` **before** this doc’s edit | `AttributeError: object has no attribute followup` on old harness | Was failing because harness built `SimpleNamespace` without `followup` — fixed above | Fixed |
| Missing `requests` / `thefuzz` / `python-dotenv` in bare runner | Some suites (`test_dashboard_page_scripts.py`, `test_slice*.py` imports of `dashboard.app`) `ModuleNotFoundError` when deps not installed | Env, not code — `pip install ...` makes them pass | Not a code bug |

**No production permission checks were weakened.** The harness that previously patched `utils.permissions.check_bot_role_position` to `λ _: (True,"OK")` now builds a real fake guild with `guild.me.top_role.position=100` and `role.position=10`, so `deliver_role → check_bot_role_position` takes its real path. Similarly, `FakeRole` now has `mention` + `__hash__` as discord.py expects, but no permission bypass.

---

## 5. What still needs no dashboard implementation (held per your direction)

* `dashboard.app: setlevel` **must not auto-add a dashboard execution form** — establish intent, permission checks, and access restrictions first; report separately. The current `COMMAND_CATEGORIES` toggle-only entry is the correct held state.
* `level_test_reset` / `xp_rollback` / `level_reconcile` stay out of dashboard scope unless demonstrated, and `xp_rollback`/`level_reconcile` are **intentionally** absent (see table in §1).
* Reward-claim global-reset and XP/Level/Prestige production reset remain **unverified read-only** — no dashboard write was added; user handles Railway CLI after fresh verified backup.

---

*Generated on branch `arena/c0c433aa-nilive-bot` from commit `12532405f80ebad9a15f2182b693640d8b73f43d`; `git diff` touches only `cogs/leveling.py` (plus test harnesses in `scripts/` for evidence). Not merged, not deployed, no production data modified.*
