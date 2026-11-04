Shop Product Management overlay (Akuroii/Nilive-Bot, PR #112)

Target base commit   : dd338082e40471495e78e7751af5bfcce7731bf6
Final source commit  : baf7af55c0ecdb964e3ef29b26c29faf575d11a8
  (code change commit: da127f74d94e10216c862ec8f970bed337e46e45; baf7af55c0ecdb964e3ef29b26c29faf575d11a8 then removes the 3 packaging files)
NOTE: these commits exist only in the local build checkout. They were NOT
pushed to GitHub and PR #112 was NOT updated (no credentials were available).
Branch arena/4db5d2ab-nilive-bot on GitHub was still at ca4149b65e9299e0117b71ff59a7ee6d3e3b082c.

FILES (paths are repository-relative, at the ZIP root; no enclosing folder)
  dashboard/api/economy_shop.py
  dashboard/templates/systems/shop.html
  scripts/test_free_shop_items.py
  scripts/test_shop_product_management.py
  scripts/test_vi_legacy_edit_form.js
  utils/shop_validation.py
  README_INSTALL.txt
  MANIFEST.sha256

WHAT CHANGED vs the previously reviewed ca4149b
  shop.html: Max stock must pass Number.isSafeInteger() (rejects Infinity from
  very long digit strings and values above 9007199254740991; previously these
  could be sent as null = unlimited, or silently rounded). Two stale
  "VI duration" comments reworded. test_vi_legacy_edit_form.js: scenarios 26-27
  (Add and Edit). The other five files are identical to ca4149b.

INSTALL (run from the ROOT of a checkout at the base commit)
  1. Back up the six files that already exist:
       mkdir -p ../overlay-backup && for f in dashboard/api/economy_shop.py \
         dashboard/templates/systems/shop.html scripts/test_free_shop_items.py \
         utils/shop_validation.py; do mkdir -p ../overlay-backup/$(dirname $f); cp $f ../overlay-backup/$f; done
     (scripts/test_shop_product_management.py and scripts/test_vi_legacy_edit_form.js
      do not exist at the base; nothing to back up for them.)
  2. unzip -o /path/to/shop-product-management-overlay.zip
  3. sha256sum -c MANIFEST.sha256        (every line must print OK)
     MANIFEST.sha256 does not list itself.
If your local copies of these files differ from the base, merge by hand
instead of overwriting.

TESTS ACTUALLY RUN (on the final files)
  pytest scripts/test_shop_product_management.py -q        45 passed
  node scripts/test_vi_legacy_edit_form.js                 27 passed, 0 failed
  node scripts/test_vi_shop_form.js                        PASS
  bash scripts/run_js_tests.sh                             25 of 26 harnesses genuinely pass; see limitations
  python3 scripts/test_dashboard_page_scripts.py           177 passed, 0 failed (needs 'acorn' installed)
  pytest scripts/test_phase1_api.py -q                     21 passed
  pytest scripts/test_shop_options.py -q                   5 passed
  pytest scripts/test_shop_categories_api.py -q            16 passed
  python3 scripts/test_prestige_api.py                     8 passed
  pytest scripts/test_free_shop_items.py -k FreeListing -q 9 passed, 14 subtests

KNOWN LIMITATIONS / PRE-EXISTING
  - python3 scripts/test_free_shop_items.py: 24 tests run, 15 ERROR, identical on
    base dd33808 and on this overlay (TypeError: safe_decrement_stock() got an
    unexpected keyword argument 'db'; cogs/shop.py calls it with db= but
    utils/economy_safe.py does not accept it). Not changed here.
  - scripts/test_moderation_smooth_navigation.js needs a live dashboard on
    127.0.0.1:5377 (Playwright); it fails without one, on base as well.
  - duration_hours in shop.html still uses the same parseInt pattern: a 400-digit
    duration becomes Infinity -> null (permanent). Out of scope; NOT fixed here.
  - The PUT /api/shop/item/<id> stock check (current_stock vs new max_stock) is
    not transactional against a concurrent refund. That endpoint is new in this PR.
  - No CI run existed for the reviewed head.
