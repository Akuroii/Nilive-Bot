Nilive-Bot session updates (single consolidated package)
Base commit : dd338082e40471495e78e7751af5bfcce7731bf6  (main at PR #111 merge)
Release tree: d065d54b13fbd855d579c1c9bb5d702d76ff09f5 (local, not pushed)
Change set  : 3 added, 8 modified, 22 deleted files (see below)

INSTALL, method A (recommended; handles deletions natively)
  git checkout dd338082e40471495e78e7751af5bfcce7731bf6        # or your main, if it still equals this base
  git apply --index nilive-bot-session-updates.patch
  git commit -m "Apply session updates"
  If main has moved past the base:  git apply --3way --index nilive-bot-session-updates.patch

INSTALL, method B (overlay + script)
  git checkout dd338082e40471495e78e7751af5bfcce7731bf6
  unzip -o nilive-bot-session-updates.zip          # run at the repository root
  sh install.sh                                    # verifies checksums, applies the 22 deletions
  rm README_INSTALL.txt MANIFEST.sha256 DELETED_FILES.txt install.sh nilive-bot-session-updates.patch
  git add -A && git commit -m "Apply session updates"
  (A plain unzip alone leaves the deleted files behind, so always run install.sh.)

VERIFY ANY TIME:  sha256sum -c MANIFEST.sha256

WHAT IS INCLUDED
 1. Shop Product Management (contents of PR #112 head f709185, 9 files):
      duration/stock must be safe integers on Add and Edit (no Infinity/NaN/400-digit/unsafe values);
      atomic stock guard on product edit; strict Prestige VI API validation;
      purchase compatibility: safe_decrement_stock/safe_deduct accept the caller db,
      give_reward accepts op_id/metadata_extra (utils/economy_safe.py, utils/reward_engine.py).
      Tests: scripts/test_shop_product_management.py, test_vi_legacy_edit_form.js,
      test_free_shop_items.py (updated), test_phase1_api.py (updated).
 2. Stage 1 (d726519): dashboard/api/tagmissions.py row_id UnboundLocalError fix (role/temp_role
      mission saves returned HTTP 500) + scripts/test_tagmissions_save.py (10 tests).
 3. Cleanup C1-C3 only:
      C1: untrack 3 committed .pyc files.
      C2: delete nilive-bot-pr106-fixes-v1.patch (already applied), P0-fixes.zip (stale),
          "__init__ (1).py" (identical to cogs/__init__.py), "leveling (1).py" (stale copy).
      C3: delete 15 root files that are byte-identical to tools/alias_probes/ copies
          (12 probes/scripts + 3 legacy probes).
 NOT included: C4-C8, any rank-card change, the PR #112 packaging files (MANIFEST, README, stale patch),
 and the PR's edits to the unused root economy_safe.py / reward_engine.py mirrors.

KNOWN LIMITATIONS (from the reported results; the full suite was NOT re-run on this combined tree)
  - test_free_shop_items.py: 3 errors remain (missing equip_engine.slot_lock x2, xp_calculator.grant_xp_boost_details x1);
    base had 15. Equip-after-purchase and XP Boost purchase paths still depend on those missing APIs.
  - test_phase2_backend.py (1 failure) and test_vi_shop_api.py (4 failures) pass on base but fail here:
    stale expectations (Prestige VI posted with max_stock=2; Title with duration; xp_boost creation)
    plus one real edge: Prestige VI with duration_hours=[] now returns 400 where base ignored it.
  - 3 root tests (test_timed_ownership, test_boost_role_race, test_pr105_fixes) fail at import on base
    (utils.inventory.find_entry does not exist); unchanged.
  - Other failures identical to base: test_afk, test_booster_rewards, test_leveling_voice_hard_blocks,
    test_shop_emoji, test_shop_emoji_tls, test_shop_publications, test_slice4_boost_claims,
    test_moderation_smooth_navigation.js (needs live dashboard + Playwright).
  - If a Prestige-VI delivery raises inside give_reward, the purchase stays 'delivering' until the reconciler (existing design).
  - DELIVERY_MANIFEST.txt still mentions the deleted PR #106 patch (docs untouched on purpose).
  - No CI run is claimed. Nothing was pushed, merged or deployed.
