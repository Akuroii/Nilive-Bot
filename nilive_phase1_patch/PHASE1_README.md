# Phase 1 Patch — P1 #14, #15, #10, #11, #12

Drop these files into your repo at matching paths, overwriting originals.
All files py_compile-clean. No untouched files included — only what changed.

## Files in this patch

| Path | What changed |
|---|---|
| `database.py` | + `economy_locks` table (unused placeholder, safe to ignore/remove later), tickets `staff_role_id` migration, `leveling_config.spam_window_seconds` migration, `ticket_settings`/`ticket_categories`/`ticket_panels` tables (were dashboard-only, now bot-readable) |
| `main.py` | + global `bot.tree.check` enforcing `command_toggles` (enabled/roles/channels/owner_only/cooldown) tree-wide |
| `cogs/customcommands.py` | + hierarchy check via `can_moderate()` before ban/kick/timeout/remove_all_roles; + bot-role-position check on `add_role:` action |
| `cogs/tickets.py` | full ticket schema migration from `ticket_config` → `ticket_settings`/`ticket_categories`; claim/close permission checks; `/ticket_close` now validates channel is a ticket first |
| `utils/economy_safe.py` | **new file** — atomic `safe_transfer`, `safe_deduct`, `safe_credit`, `safe_decrement_stock` using `BEGIN IMMEDIATE` |
| `cogs/economy.py` | `/give`, `add_balance`, `removecoins` now use atomic ops — closes overdraft race |
| `cogs/shop.py` | `process_purchase` now claims stock + deducts balance atomically, refunds stock on failed payment |
| `cogs/leveling.py` | + frequency-based spam detection (`spam_threshold` msgs / `spam_window_seconds`) with XP penalty; voice XP task now has per-member try/except so one bad row doesn't kill the guild's tick |
| `utils/xp_calculator.py` | default config dict + `spam_window_seconds` key |

## Apply order

1. Stop the bot.
2. Copy files over (paths above match your repo exactly).
3. Delete your `.db` file **only if** you want a completely fresh start — otherwise migrations in `database.py` handle existing DBs automatically on next `init_db()` call.
4. Restart. Watch logs for `[MIGRATION]` lines confirming schema updates ran.
5. Test in this order: `/ticket_setup` → open a ticket → claim it → close it. Then `/give` between two test accounts rapid-fire (5x in under 1s) to confirm no overdraft. Then spam a channel fast to confirm XP stops accruing and `spam_xp_penalty` deducts.

## Not yet touched (next phase)

P1 #13 (moderation logging field alignment), P1 #16 (report system, new), P1 #17 (health dashboard, new). Say "go" to continue.
