Shop Product Management overlay (Nilive-Bot)

Expected base commit : dd338082e40471495e78e7751af5bfcce7731bf6
Final (local) commit : a86580f41e32ded0e4efedb6e484e54f445e0566   (NOT pushed; no PR update)

Contents: six complete final files (repo-root-relative paths), MANIFEST.sha256,
and a binary git patch (base -> final) as a recovery/reference copy.

Install (preferred), from the REPOSITORY ROOT at the base commit:
  unzip shop-product-management-overlay.zip -d /tmp/overlay
  cp -r /tmp/overlay/shop-product-management-overlay/dashboard /tmp/overlay/shop-product-management-overlay/scripts /tmp/overlay/shop-product-management-overlay/utils .
  (cd /tmp/overlay/shop-product-management-overlay && sha256sum -c MANIFEST.sha256)

Alternative (patch):
  git apply --check /tmp/overlay/shop-product-management-overlay/*.patch && git apply /tmp/overlay/shop-product-management-overlay/*.patch
Use only ONE method. If your local copies of these six files differ from base,
review and merge by hand instead of overwriting.

Tests: node scripts/test_vi_legacy_edit_form.js ; pytest scripts/test_shop_product_management.py
Known baseline (not part of this change): 15 failures in full scripts/test_free_shop_items.py
(safe_decrement_stock(db=...)). Documented low-severity caveat: the max_stock vs
current_stock check in update_shop_item is not transactional against a concurrent refund.
