# Shop Designer / Publication fixes: install package

**Base:** `origin/main` commit `9c63a4811488a7c287ca4790584f695e78d3bb2d` ("Add files via upload (#85)"). Built and tested on a pristine checkout of that commit.
**Contents:** 16 files (9 modified, 7 new): 8 runtime files and 8 test files. Nothing else. No deletions, no root-file cleanup, no schema changes.
**Not committed, pushed or deployed by me.** Nothing here has touched production or a real Discord server.

## What it fixes
- **Unpublish / Delete stuck on a deleted channel:** a record is cleared only when Discord confirms the channel is gone (server channel list lacks it AND `GET /channels/<id>` returns 404 / code 10003). A 403, timeout, rate limit or permission error keeps the record protected.
- **Discord deletion events:** deleting a message, bulk-deleting, or deleting a channel cleans only the exact affected publication records (idempotent; in-flight operations are skipped).
- **Stale operations:** `updating` / `unpublishing` / `replacement_sending` rows older than 15 minutes (a crash or restart) are moved to a recoverable state once at bot startup.
- **Re-check with Discord** (new button): clears only records Discord confirms are gone and reports anything it could not verify.
- **Remove record only** (new, explicit confirmation): database-only removal for `pending`, `attention` and `replacement_send_uncertain` rows. Discord is never contacted; the message, if any, must be deleted manually. Refused for `published` rows.
- **Save → Publications:** the Publications panel now follows the design selected by Save and Delete.
- **Server error messages:** Preview and Save show the server's reason instead of "HTTP 400/409"; a blocked Update lists every validation problem.
- **Token Catalog in Shop Designer:** a collapsible panel (search, groups, aliases, copy) built from the catalog the Designer already fetches. This is an *addition*: the Token Catalog on the legacy Publisher page is left in place.
- **Unchanged:** the `design_has_publications` delete guard.

## Legacy Shop Publisher: NOT touched
The legacy Shop Publisher page, its navigation, its routes and Category Assignment are not removed, hidden or modified. No removal has been implemented or reviewed.

## File manifest (destination = same path inside your project root)
| # | Destination path (relative to project root) | Change | Kind | Purpose | SHA-256 |
|---|---|---|---|---|---|
| 1 | `cogs/shop_publication_sync.py` | NEW | runtime | Discord deletion-event sync (message, bulk, channel) plus one-time recovery of interrupted operations at startup. | `c583bca5614074c4…` |
| 2 | `main.py` | MOD | runtime | One line: registers the cog above in the cog loader. | `c6a2006961bcc064…` |
| 3 | `utils/shop_publication.py` | MOD | runtime | Unpublish clears a record when its channel is confirmed deleted; reconcile helpers; stale-operation recovery; Discord re-check; record-only removal. | `ec0ba290de5a8bf6…` |
| 4 | `dashboard/api/shop_publications.py` | MOD | runtime | New routes: re-check a design's publications, confirmed record-only removal; unpublish reports channel_deleted. | `316ea8a902b72e2f…` |
| 5 | `dashboard/static/js/shop-publications.js` | MOD | runtime | Re-check button, Remove record only action (with confirmation), honest status text, shows every validation problem on a blocked Update. | `592e89cf8b18edf4…` |
| 6 | `dashboard/static/js/shop-designer.js` | MOD | runtime | Token Catalog panel; Publications panel follows Save/Delete selection; Preview and Save show the server's reason instead of HTTP 400. | `dcb5fd00b91a9132…` |
| 7 | `dashboard/templates/manage/shopdesigner.html` | MOD | runtime | Token Catalog panel markup and the Re-check with Discord button. | `3f44f8130723646c…` |
| 8 | `dashboard/static/css/shop-designer.css` | MOD | runtime | Styles for the Token Catalog panel. | `00bbd392b0923be0…` |
| 9 | `scripts/test_shop_publication_reconcile.py` | NEW | test | 30 tests: deleted channel, event reconcilers, cog handlers, stale recovery, re-check, record-only removal. | `47e361c84bf8b0f8…` |
| 10 | `scripts/test_shop_failed_publication_unpublish.py` | NEW | test | 9 tests: failed/message-less rows, pending protection, published guard, normal delete. | `1ed272784ebc7b9e…` |
| 11 | `scripts/test_shop_designer_token_catalog.js` | NEW | test | 25 checks: Token Catalog panel against the real server catalog. | `d1ca670d40998857…` |
| 12 | `scripts/test_shop_designer_preview_save_publications.js` | NEW | test | 23 checks: Preview/Save error reasons and Save/Delete selection sync. | `c19c3085e19f9be5…` |
| 13 | `scripts/test_shop_publication_recovery_ui.js` | NEW | test | Re-check and Remove record only UI behavior. | `1e47e8c8bdd2020a…` |
| 14 | `scripts/test_shop_failed_publication_ui.js` | NEW | test | Failed message-less row shows only Unpublish. | `36126be0585454c0…` |
| 15 | `scripts/test_shop_publications_ui.js` | MOD | test | 3 assertions updated for the new record-only button and failed-row behavior. | `4ee629527509d1d0…` |
| 16 | `scripts/test_shop_designer_persistence.js` | MOD | test | Mock ctx.fetch now behaves like the real one (serves every route as a Response). | `3399d56cb16d9416…` |

Full hashes: `_install/FILES.sha256` (verify with `sha256sum -c _install/FILES.sha256` from the project root). `_install/designer-publication-fixes.patch` is the same change as a git patch (SHA-256 `3370afa9f5e86c3d94a427d510fa527320d6f353a1b5b6e8c9f86bb5dc4969dc`).

## Install
1. **Check for local changes first.** This ZIP replaces whole files. If you have **uncommitted edits to any of the 9 modified files** (for example local Token Catalog work in `shop-designer.js` or `shopdesigner.html`), do NOT unzip over them. Use the patch instead: `git apply --3way _install/designer-publication-fixes.patch`.
2. Back up the 9 modified files (copy them somewhere, or make sure they are committed).
3. From the project root: `unzip -o shop-designer-publication-fixes.zip`
4. Verify: `sha256sum -c _install/FILES.sha256` (all lines `OK`). You may delete the `_install/` folder afterwards.
5. **Restart both the bot and the dashboard** (on Railway: redeploy; `start.sh` starts both). The bot restart is required to load the new cog and `main.py`; the dashboard restart loads the Python changes. Static JS/CSS/template changes are picked up on the next page load (the server revalidates; do a hard refresh once).
6. Optional: check the bot log for no cog load error for `cogs.shop_publication_sync`.

## Configuration / migration
None. No environment variables, no database migration (the schema is unchanged). The bot already runs with `Intents.all()`, which covers the deletion events. Re-check uses the same bot permissions as existing publication operations (View Channel, Read Message History).

## Tests run (on a pristine `9c63a48` checkout with this ZIP installed; Discord faked)
- npm test (all JS harnesses): 17 harness(es) passed.
- python scripts/test_shop_publication_reconcile.py: Ran 30 tests in 7.499s OK 
- python scripts/test_shop_failed_publication_unpublish.py: Ran 9 tests in 2.274s OK 
- python scripts/test_shop_designs_api.py: Ran 22 tests in 4.656s OK 
- python scripts/test_shop_publisher_api.py: Ran 18 tests in 4.129s OK 
- python scripts/test_shop_publisher.py: 115/115 passed. 
- python scripts/test_shop_categories_api.py: Ran 16 tests in 3.753s OK 
- python scripts/test_shop_json_body_api.py: Ran 1 test in 0.303s OK 
- python scripts/test_dashboard_page_scripts.py: DASHBOARD PAGE SCRIPTS: 148 passed, 0 failed (36 routes) 
- pytest scripts/test_shop_publications.py: 23 passed, 1 warning in 0.82s
- node scripts/test_shop_designer_token_catalog.js: 25/25 checks passed.
- node scripts/test_shop_designer_preview_save_publications.js: 23/23 checks passed.
- node scripts/test_shop_publication_recovery_ui.js: All publication recovery UI checks passed.
- node scripts/test_shop_failed_publication_ui.js: All failed-publication UI checks passed.
- node scripts/test_shop_publications_ui.js: All Publication UI regression checks passed.
- browser E2E designer (token panel, save->publications, preview reason, re-check, record-only): 24/24 checks passed 
- browser E2E failed-row unpublish + pending protection: 15/15 checks passed 
- browser E2E publisher delete (409 reason, failures): 24/24 checks passed

Run them yourself from the project root: `npm test` and `OWNER_ID=1 DISCORD_TOKEN=x PYTHONPATH=. python3 scripts/<name>.py` (for `test_shop_publications.py` use `python3 -m pytest -q scripts/test_shop_publications.py` with the same variables). This repo tracks `__pycache__/*.pyc`, which running tests rewrites; restore them with `git checkout -- '*.pyc'` before committing.
The three browser checks use a headless Chromium with the real Flask app and SQLite; their scripts are provided separately and are not part of this ZIP.

## Known limitations / NOT verified
- **Nothing was verified against a real Discord server or production.** Every Discord call was faked. Live behaviour of Update, Unpublish, Re-check and the event handlers is unconfirmed.
- Event handlers only see deletions that happen **while the bot is online**; missed ones are recovered with Re-check.
- I could not confirm that Discord's channel list is unfiltered by the bot's view permission; the second confirmation request (`GET /channels/<id>` = 404 / 10003) exists to cover that.
- The real state of your design 1/2 records and the exact body of your earlier HTTP 400 are unknown, so I cannot say this fixes those specific records. Use **Re-check** or **Unpublish** on them, and **Remove record only** if those cannot resolve a stuck record.
- Preview: I added error-path tests and audited the code; I did not independently re-verify every action type beyond the existing suite.
- The root-level duplicate files are not addressed here (see `root-duplicates-report.csv`, delivered separately).

## Rollback
Using git, from the project root (restores the 9 modified files and removes the 7 new ones):
```
git checkout 9c63a48 -- main.py utils/shop_publication.py dashboard/api/shop_publications.py dashboard/static/js/shop-publications.js dashboard/static/js/shop-designer.js dashboard/templates/manage/shopdesigner.html dashboard/static/css/shop-designer.css scripts/test_shop_publications_ui.js scripts/test_shop_designer_persistence.js
rm cogs/shop_publication_sync.py scripts/test_shop_publication_reconcile.py scripts/test_shop_failed_publication_unpublish.py scripts/test_shop_designer_token_catalog.js scripts/test_shop_designer_preview_save_publications.js scripts/test_shop_publication_recovery_ui.js scripts/test_shop_failed_publication_ui.js
```
Then restart the bot and dashboard. Or restore the 9 files from your backup from step 2. Records removed by Re-check, event handlers or Remove record only are **not** restored by a rollback (they are deleted rows); the rollback only returns the code.
