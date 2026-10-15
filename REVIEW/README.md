# D1-D6 review package

Status: **for review and testing only.** Not committed, pushed, merged or deployed. `main` is untouched.
Base: branch `arena/b5a56831-nilive-bot` at commit `99851bf` (verified).
**Independent of P-1.** The P-1 fix (`list_claims`, separate ZIP) is NOT included or applied here.

## Changes (2 files, +88 / -16)
| # | File | Change |
|---|---|---|
| D1 | `utils/level_claims.py` | New `_confirm_role_added()`: when discord.py's member cache does not yet show a role that `add_roles` just accepted, ask Discord (`guild.fetch_member`). Confirmed -> no false "Role add returned without membership" failure. Not confirmed (role absent, REST error, no `fetch_member`) -> still reported failed. No extra call when the cache is already correct. |
| D2 | `utils/level_claims.py` | Per-pass `attempted_ids` set in `reconcile_role_progression`: one add attempt per role per pass (a role shared by the Member and Booster partitions, or configured at several Levels, was added twice under cache lag). A failed first attempt is not retried in the same pass; its failure stays reported. |
| D3 | `dashboard/templates/systems/leveling.html` | `applyRewardFilter()` runs in the empty branch of `loadCurrencyRewards`, `loadShopRewards`, `loadBoostRewards`, so the filter summary refreshes when a table becomes empty. |
| D4 | `leveling.html` | `rewardApiCall()` never throws. `addReward`/`deleteReward` show an error toast for network failures, non-JSON responses (with HTTP status) and server error text. |
| D5 | `leveling.html` | `loadRewards()` resolves true/false. After a successful add/delete, a failed refresh shows one warning ("...table could not be refreshed. The list shown may be out of date; reload the page.") instead of an operation error. |
| D6 | `leveling.html` | `__rewardsLoadSeq` guard: only the newest `loadRewards` response may render. |

D4-D6 cover the **role-reward** path only. Untouched: tests, scripts, `/level` visuals, reward eligibility, claim ownership, reservation logic, schema, B5/P1 code, P-1.

## Manifest
| Path | Deploy? | Base blob (99851bf) | Result blob | SHA-256 |
|---|---|---|---|---|
| `utils/level_claims.py` | Yes | `66c42f0e212efb9c14fea38ba62fe86c4ca08629` | `71c7386877209a4448910d3e988b5788da6e4b57` | `114d6cf5e0af2a680797b1ad87baae553f7f65a1ebb0127a233487e079049bf1` |
| `dashboard/templates/systems/leveling.html` | Yes | `2d63be819901ba7bc802cd918c0c2d4907b2d010` | `bf6931c14d4c4cb412afc9c0e04accab286c5bbb` | `4a04c2c1493de9bc600d046f467266cc507bbf73235d51de3dffec1104d46bcd` |
| `REVIEW/D1-D6.diff` | No (documentation) | | | |
| `REVIEW/README.md` | No (documentation) | | | |

No tests, scripts, `.pyc`, or other files are included.

## Verification (run by the reviewer in throwaway worktrees; no test or script modified)
Environment: Python 3.12.3, discord.py 2.7.1, Node 22 + jsdom 24, fake Discord objects, real DB code.
- **Existing Python suites (14), baseline 99851bf vs D1-D6: identical, all exit 0, 0 failures** - booster role strip 25, booster rewards 41, XP safety 124/124, role progression 46/46, rejoin 24, Claim All 25, runtime 25, reward e2e 11, caller integrations 11, slice 2/3/4 26/21/15, final fixes 21, card visuals 52.
- **D1/D2 probe** (reviewer scratch, fake member reproducing discord.py cache lag): baseline 9/14 (the 5 failures are exactly D1/D2, e.g. `adds=[802, 802]`), D1-D6 **14/14**. Includes: genuine silent failure still failed; REST-confirm error still failed; real add error still failed; non-booster gets no Booster role; only fulfilled claims restored; `exclude_role_ids` honoured; Exclusive ON restores only the highest set.
- **D3-D6 jsdom check** (real functions extracted from the template): baseline 5/20, D1-D6 **20/20**.
- **B5/P1 regression** (reviewer scratch): 19/19 + 2 Exclusive-ON shared-role cases.
- **Existing dashboard harnesses (U1/U2/U3):** identical before and after (known stub-DOM limitation: U1 2 failing checks, U3 2 failing checks, U2 TypeError). `test_dashboard_page_scripts` exits 0 on both; pass count not extracted. U2/U3 need `/usr/local/bin/node` (sandbox symlink used).
- The probes/harness are reviewer scripts and are intentionally NOT in this ZIP.

## Apply
Only onto a branch whose files are exactly the base blobs:
```
git hash-object utils/level_claims.py                       # 66c42f0e212efb9c14fea38ba62fe86c4ca08629
git hash-object dashboard/templates/systems/leveling.html   # 2d63be819901ba7bc802cd918c0c2d4907b2d010
git apply --check REVIEW/D1-D6.diff
git apply REVIEW/D1-D6.diff
```
If either hash differs, do NOT overwrite the file with the ZIP copy; use the diff. Built on `99851bf`; current `main` is older and will not take it as-is.
P-1 compatibility: P-1 edits `list_claims` (base lines ~1066-1092); D1/D2 edit lines ~430-715 of the same file. The hunks do not overlap, so they should be independently applicable, but **the combination was not applied or test-run**.

## Rollback
```
git apply -R REVIEW/D1-D6.diff
# or restore base files:
git checkout 99851bf -- utils/level_claims.py dashboard/templates/systems/leveling.html
```
No schema or data change; no migration.

## Known limitations / risks
1. Not verified in real Discord or a real browser; Python 3.11 (Dockerfile image) untested.
2. D1 adds one REST GET per restored role (under discord.py 2.7.1 cache lag, effectively every restore). Low volume, but new.
3. If the D1 confirmation call itself fails after a successful add, the role is still reported failed (safe direction; the old false message can still appear in that rare case).
4. Exclusive ON: the later removal check re-reads the stale cache and still skips removal in that pass. Unchanged on purpose (changing it would alter restoration rules).
5. D2 side effect: duplicate `failed`/`restored` entries for one role become single entries; no existing suite depended on duplicates.
6. D5: the success toast now appears after the refresh completes. A superseded refresh (D6) resolves true by design, so a stale list is silent if the newer load has no reporting caller (e.g. page init).
7. D4-D6 role-reward path only; the currency/shop/boost add/delete handlers still have unguarded `res.json()`.

## Separate open findings (not part of this package)
P-1 (expired `processing` claims hidden in `/level`; own ZIP); event-driven Booster re-boost recovery (missed event while bot offline); Claim All locked-Booster footer count in the P-1 scenario; strip/restore ordering race; config-deletion edge case; admin-removed shared roles later restored by the Member path.
