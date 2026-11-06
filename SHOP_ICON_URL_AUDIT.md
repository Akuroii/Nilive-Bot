# Shop Icon URL investigation and repair

Date: 2026-11-06 (UTC)
Repository: `Akuroii/Nilive-Bot`
Session branch: `arena/3ae570ab-nilive-bot`
Audited base HEAD: `b320ba76437b53f3e17008f54cf836c5b89307ec`

## 1. PR and branch state verified before edits

- PR #120 (`arena/01a0a75e-nilive-bot`) is **MERGED**, not open.
- PR head: `1155540fe42aa776e3367ac175156be5cceef2a2`.
- Merge commit / `main` at audit time: `b320ba76437b53f3e17008f54cf836c5b89307ec`.
- `git diff FETCH_HEAD HEAD` after fetching `refs/pull/120/head` was empty: this checkout already contained the reviewed PR #120 code.
- Because #120 is merged, its branch/PR cannot be safely updated as an open review vehicle. This repair was made on the fixed Arena session branch. No merge, deployment, Railway change, or production database access was performed.

## 2. Discord rendering constraint

An arbitrary HTTP(S) image URL is not an inline Discord emoji. Inline custom emoji require the `<:name:id>` / `<a:name:id>` token; application-owned emoji are usable by the owning application without `USE_EXTERNAL_EMOJIS`.

Official references consulted:

- Discord Emoji Resource: https://discord.com/developers/docs/resources/emoji
- Discord Message Formatting reference: https://docs.discord.com/developers/reference

This matches the repository's existing design: Shop Dashboard saves `icon_url`; the owner explicitly prepares a normalized application emoji; Inventory may only render an already-`ready` prepared emoji token. A raw URL remains valid for Rank's Pillow image fetch and Inventory's item-detail thumbnail, but not for an inline Inventory row.

## 3. Proven code-level root cause of the reproduced failure

The shared read-only resolver, `utils/shop_emoji.py::attach_prepared_emojis`, selected the application **only** from `DISCORD_CLIENT_ID`:

- pre-fix `utils/shop_emoji.py:732-744`: `application_id = configured_application_id()`; if empty, return before any alias SQL.
- pre-fix `cogs/wallet.py:186-241` and `:1177`: Inventory never passed the interaction client's authenticated `application_id`.
- pre-fix `utils/rank_card_data.py:38-40`, `:171`, `:207`: Rank aggregation also never accepted/passed that identity.
- pre-fix `cogs/leveling.py:2316-2327`: the real `/rank` callback did not pass `interaction.client.application_id`.

Therefore, with a correctly saved URL, a correctly scoped `ready` alias, and an authenticated bot client whose `application_id` matches that alias, Inventory still produced `app_emoji=""` whenever the bot process lacked (or had a stale) `DISCORD_CLIENT_ID`. `cogs/wallet.py::_item_display` then correctly fell back to the rarity glyph, producing the reported white circles. Rank lost the same prepared-emoji source and could only try raw URL fallbacks.

The four-product scratch reproduction emitted exactly this before the fix:

```text
> - ⚪ • ` 1 ` • ` عنتيل السيرفر ` • *Common*
> - ⚪ • ` 1 ` • ` عصير المجرات ` • *Common*
> - ⚪ • ` 1 ` • ` شوكولاته الجلاكسي ` • *Common*
> - ⚪ • ` 1 ` • ` سهران السيرفر ` • *Common*
```

Important attribution boundary: this is a **proven failure mode in the audited code**, reproduced with the four requested product names. Production DB/env were not inspected, so this report does not claim to have observed the deployed Railway values. If the deployed bot already has the correct `DISCORD_CLIENT_ID`, this specific defect would not by itself explain that deployment; the remaining production checks are listed in section 8.

## 4. Smallest safe fix implemented

Thread the authenticated bot application ID through the existing read-only resolver, without changing the explicit preparation workflow:

- `utils/shop_emoji.py::attach_prepared_emojis(..., *, application_id=None)` now uses the explicit caller-supplied ID when present; otherwise it preserves the old `DISCORD_CLIENT_ID` default for dashboard/previews/tests.
- `cogs/wallet.py` passes `interaction.client.application_id` through `_decorate` / `_attach_prepared_app_emojis` for the Inventory list, item detail, and drop-confirm paths.
- `utils/rank_card_data.py::get_rank_card_data(..., *, application_id=None)` passes the same identity to both prepared-emoji lookup passes.
- `cogs/leveling.py::Leveling.rank` passes the client identity to both the normal render and fallback aggregation calls.

Preserved unchanged:

- URL save endpoints and `shop_items` / `item_catalog` persistence.
- Explicit owner-only Shop emoji preparation/reconciliation endpoints.
- Canonical URL rules, guild scoping, exact-name ambiguity guard, and `status='ready'` requirement.
- Rank raw-image fallback order: prepared CDN -> unique listing URL -> catalog URL -> placeholder.
- Inventory layout, ownership, timed items, purchases, equip behavior, schema, and migrations.
- No upload, backfill, recovery, DDL, or DB write occurs when Inventory/Rank is opened. Full-table scratch snapshots before/after navigation/render were equal.

## 5. Files changed

| File | Why |
|---|---|
| `utils/shop_emoji.py` | Optional explicit application identity for the existing read-only alias lookup; env fallback preserved. |
| `cogs/wallet.py` | Propagate authenticated client identity through all Inventory decoration entry points. |
| `utils/rank_card_data.py` | Accept/propagate the same identity for Rank grid prepared-emoji resolution. |
| `cogs/leveling.py` | Real `/rank` callback supplies the client identity. |
| `scripts/test_shop_icon_resolution.py` | New isolated four-product save -> explicit prepare -> Inventory/Rank regression, including missing/stale env, non-ready/missing aliases, stale catalog, wrong guild/application, ambiguity, canonical URL/query semantics, and no-mutation checks. |
| `scripts/test_inventory_panel.py` | Footer-test mock accepts the new keyword-only decoration argument; no product behavior changed. |
| `scripts/test_inventory_six_tabs.py` | Two mocks accept the new keyword; balance assertion matches PR #120's description-row contract; added the missing `unittest.main()` entry point so `python scripts/test_inventory_six_tabs.py` actually runs the suite instead of exiting silently. |

## 6. Reproduction and regression evidence

Environment: isolated `.venv`, production requirements installed unchanged plus test-only `pytest`/`pytest-asyncio`. Scratch DBs only; Discord/source network I/O blocked in the new regression. Preparation used an in-memory fake application through the real owner-only endpoint.

Pre-fix proof used an archived copy of base HEAD `b320ba7` plus the final regression file:

```bash
git archive HEAD | tar -x -C /home/user/nilive-icon-audit/baseline-tree
cp scripts/test_shop_icon_resolution.py /home/user/nilive-icon-audit/baseline-tree/scripts/
cd /home/user/nilive-icon-audit/baseline-tree
PYTHONPATH=. /home/user/Nilive-Bot/.venv/bin/python scripts/test_shop_icon_resolution.py
# exit 1: Ran 11 tests; FAILED (failures=3, errors=7)
```

The failures include the exact white-circle Inventory rows above, missing runtime identity in item detail/drop confirm, and Rank's inability to accept/propagate `application_id`.

Fixed tree:

```bash
PYTHONPATH=. .venv/bin/python scripts/test_shop_icon_resolution.py
# exit 0: Ran 11 tests in 12.097s; OK
```

Surrounding suites actually executed after the repair:

```bash
PYTHONPATH=. .venv/bin/python scripts/test_inventory_panel.py
# exit 0: Ran 30 tests; OK

PYTHONPATH=. .venv/bin/python scripts/test_inventory_six_tabs.py
# exit 0: Ran 9 tests; OK

PYTHONPATH=. .venv/bin/python scripts/test_inventory_entry_safety.py
# exit 0: Ran 9 tests; OK

PYTHONPATH=. .venv/bin/python scripts/test_inventory_activity.py
# exit 0: Ran 16 tests; OK

PYTHONPATH=. .venv/bin/python scripts/test_wallet_phase2.py
# exit 0: Results: 54 passed, 0 failed

PYTHONPATH=. .venv/bin/python scripts/test_rank_icons.py
# exit 0: ALL RANK ICON CASES PASSED

# Run from a scratch cwd because this harness writes preview PNGs into cwd.
cd /home/user/nilive-icon-audit
PYTHONPATH=/home/user/Nilive-Bot RANK_IT_DB=/home/user/nilive-icon-audit/rank-integration.db \
  /home/user/Nilive-Bot/.venv/bin/python /home/user/Nilive-Bot/scripts/test_rank_integration.py
# exit 0: ALL INTEGRATION CASES PASSED

PYTHONPATH=. .venv/bin/python -m pytest -q scripts/test_shop_emoji.py
# exit 0: 93 passed

PYTHONPATH=. .venv/bin/python scripts/test_shop_emoji_api.py
# exit 0: Ran 8 tests; OK

PYTHONPATH=. .venv/bin/python scripts/test_shop_product_management.py
# exit 0: Ran 50 tests; OK

npm test
# exit 0: 26 harness(es) passed

.venv/bin/python -m py_compile utils/shop_emoji.py cogs/wallet.py \
  utils/rank_card_data.py cogs/leveling.py \
  scripts/test_shop_icon_resolution.py scripts/test_inventory_panel.py \
  scripts/test_inventory_six_tabs.py
# exit 0

git diff --check
# exit 0

cmp -s AGENTS.md CLAUDE.md
# exit 0
```

Non-product verification issues encountered and resolved:

- Initial `pytest scripts/test_shop_emoji.py` without `OWNER_ID` exited during import (`SystemExit`); rerun with isolated test env passed.
- Combining `test_shop_emoji_api.py` with an externally supplied `DATABASE_PATH` tripped its scratch-only guard; its documented direct script entry point passed.
- Two existing `_decorate` mocks rejected the new keyword; updated mocks only.
- `test_inventory_six_tabs.py` had no executable unittest entry point and initially produced a misleading exit 0 with zero tests; added the runner and then executed all 9 tests.
- The first Rank integration run regenerated tracked `preview_p4_*.png` artifacts; those test-generated bytes were restored from HEAD, and the final integration run used a scratch cwd. No preview artifact changes remain.

## 7. Fixed status

- **Inventory:** fixed for the demonstrated identity-resolution failure. A valid, guild-scoped, application-scoped `ready` alias now reaches the row even when the bot process lacks or has a stale `DISCORD_CLIENT_ID`. Missing/non-ready/ambiguous/wrong-scope cases still fall back safely and do not mutate state.
- **Rank:** fixed for the same shared identity failure. The real `/rank` callback now supplies the authenticated application ID, and the renderer requests the prepared Discord CDN asset. Existing raw URL fallback behavior is unchanged.
- **Layout/ownership/purchases:** unchanged.

## 8. Remaining unverified / production acceptance checks

Not performed by design: production DB inspection, Railway configuration changes, deployment, live Discord gateway/client rendering, real Discord emoji upload, or PR merge.

Before claiming the deployed incident closed, verify on the deployed build:

1. The running bot includes this repair commit.
2. Each of the four Shop products has the intended HTTPS `icon_url` saved.
3. The owner has explicitly run Shop Designer preparation/reconciliation for those products and the result is `ready` with a bound emoji ID. Saving a URL alone is intentionally insufficient.
4. The prepared alias belongs to the same guild and the bot token's application. The repaired bot read now uses the authenticated client ID rather than depending on the dashboard env variable.
5. The live Discord client displays the prepared application emoji in Inventory and the Rank grid. Local tests prove token/CDN selection and PNG rendering with controlled fixtures, not Discord's live client pixels.

If those conditions hold and icons still fail, capture the prepared result status/code and the bot's deployed commit before changing anything else; do not add automatic uploads or weaken the `ready`/scope guards.

## 9. Delivery

Implementation and verification are committed on `arena/3ae570ab-nilive-bot`. The final commit SHA and follow-up PR status are reported in the delivery message. PR #120 remains merged; it was not reopened, merged again, or deployed.
