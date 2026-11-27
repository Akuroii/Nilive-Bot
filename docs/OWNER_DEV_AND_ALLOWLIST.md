# OWNER / DEV tiers and the server allowlist

## Railway variables

| Variable | Who | Notes |
|---|---|---|
| `OWNER_ID` | the bot's real owner | required; one ID or a comma-separated list |
| `DEV_ID` | the developer | optional; same format. **Unset = OWNER_ID is also the developer** (old behavior) |
| `ALLOWED_GUILD_IDS` | approved servers | comma-separated; **empty = feature off** |
| `GUILD_ALLOWLIST_MODE` | `off` / `log` / `enforce` | default `log` (prints only). Unknown value falls back to `log` |

## What each tier gets

| | OWNER | DEV |
|---|---|---|
| Dashboard in every installed server, backups page | yes | yes |
| Discord `/backup_now`, `/backup_list` | yes | yes |
| Visible `owner` row in each server's Access page | **yes** | no (hidden bypass, as before) |
| `/dev_leave_guild`, `!sync`, `!reload` | no | **yes** |

`bot.is_owner()` now follows `OWNER_ID` + `DEV_ID` instead of the Developer Portal application owner.

## Safe rollout order

1. Deploy this code with no variable changes (nothing changes: `DEV_ID` unset means OWNER_ID is still the developer).
2. Set `DEV_ID` = developer ID **and** `OWNER_ID` = owner ID together, one deploy.
3. Set `ALLOWED_GUILD_IDS` (leave `GUILD_ALLOWLIST_MODE` unset = `log`), read the Railway logs for `[ALLOWLIST]` lines for a few days.
4. Only then set `GUILD_ALLOWLIST_MODE=enforce`.

Setting `OWNER_ID` to the owner before step 1 is deployed would hand the old hidden-admin power to the owner and remove it from the developer.

## Allowlist safety rules

- Unparsable list: feature disabled with a warning. Never `enforce` on a typo.
- Startup sweep in `enforce` refuses to run if none of the allowed servers is one the bot is in.
- A server on `ALLOWED_GUILD_IDS` cannot be left with `/dev_leave_guild`.
- Leaves are logged as `[GUILD-ADMIN]` / `[ALLOWLIST]` lines in the Railway logs.

Tests: `python tests/test_owner_dev_tiers.py`
