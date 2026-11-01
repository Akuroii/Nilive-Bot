# Three-way archive decisions

Base: `a6b812b`; old contents: verified 28-file archive; candidate: `8eb8e3d`.

19 files are byte-identical, one has documentation-only differences, eight have newer implementations/tests. No archive file was overlaid. These are archive restoration decisions, not closure of Phase 2 guarantees.

| File | Decision | Evidence/reason |
|---|---|---|
| `cogs/shop.py` | superseded_by_newer | Newer implementation: operation-linked boosts and member-locked legacy expiry; no old function removed. |
| `cogs/trade.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `cogs/wallet.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `dashboard/templates/systems/shop.html` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `database.py` | superseded_by_newer | Newer implementation: required origin_op_id migration and boost provenance column. |
| `scripts/test_free_shop_items.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `scripts/test_vi_shop_api.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `scripts/test_vi_shop_form.js` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `scripts/test_wallet_phase2.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `test_free_shop_items.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `test_vi_shop_api.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `test_wallet_phase2.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `utils/economy_safe.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `utils/equip_engine.py` | superseded_by_newer | Newer implementation: per-member slot locks and identity-conditional slot cleanup. |
| `utils/inventory.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `utils/level_claims.py` | superseded_by_newer | Newer implementation: shared-role preservation in progression and unboost. |
| `utils/level_test_reset.py` | already_present | Existing implementation; candidate difference is ownership-policy documentation only. |
| `utils/potion_engine.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `utils/rank_card_data.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `utils/reward_engine.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `utils/shop_validation.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `utils/title_engine.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `utils/xp_calculator.py` | superseded_by_newer | Newer implementation: origin_op_id in boost inserts and API forwarding. |
| `scripts/recover_shop_purchases.py` | superseded_by_newer | Newer implementation: operator safety guidance and non-owner sentinel. |
| `scripts/test_inventory_ui.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `test_inventory_ui.py` | already_present | Archive and candidate are byte-identical: no restoration needed for this file. This is not a functional certification. |
| `test_timed_ownership.py` | superseded_by_newer | Newer regressions; old P38/P39 legacy inference expectations must not overwrite fail-closed provenance checks. |
| `utils/timed_ownership.py` | superseded_by_newer | Newer ownership boundary, provenance, reconciliation, retirement and synchronization. Removed journal-only holding inference is intentionally not restored. |

Full base/archive/candidate content hashes: [archive-three-way.json](archive-three-way.json).

UI/API review boundary: preserved free-item form through its fetch request and API validation; currency-aware trade/wallet display; session-guild and admin guards in dashboard/api/economy_shop.py. Executed tests cover selected real Flask routes and JS form harnesses. No browser/Discord integration certification is claimed. The archive is not a recovered Phase 2.2 patch.
