#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Shop Publisher — Legacy page retirement verification.

   The standalone /shop-publisher page has been retired.
   This test verifies:
   - The template no longer exists
   - Shop Designer still uses the shared shop-publisher.js
   - Integrated Publish/Publications workflow remains covered by existing tests

   The meaningful assertions from the old publisher-form harness are now covered by:
   - scripts/test_shop_designer_catalog.js
   - scripts/test_shop_designer_preview.js
   - scripts/test_shop_designer_preview_save_publications.js
   - scripts/test_shop_publications_ui.js
   - scripts/test_shop_designer_actions.js
   - scripts/test_shop_designer_token_catalog.js

   Run:  node scripts/test_shop_publisher_form.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }

const ROOT_DIR = path.join(__dirname, '..');

section('Legacy /shop-publisher page retirement');

// 1. Verify the standalone page template is removed
const SHOP_PUBLISHER_TEMPLATE = path.join(ROOT_DIR, 'dashboard', 'templates', 'manage', 'shoppublisher.html');
assert(!fs.existsSync(SHOP_PUBLISHER_TEMPLATE),
    'shoppublisher.html template is removed');

// 2. Verify Shop Designer template still exists
const SHOP_DESIGNER_TEMPLATE = path.join(ROOT_DIR, 'dashboard', 'templates', 'manage', 'shopdesigner.html');
assert(fs.existsSync(SHOP_DESIGNER_TEMPLATE),
    'shopdesigner.html template still exists');

// 3. Verify Shop Designer still loads shared shop-publisher.js
const SHOP_DESIGNER_HTML = fs.readFileSync(SHOP_DESIGNER_TEMPLATE, 'utf8');
assert(SHOP_DESIGNER_HTML.includes('shop-publisher.js'),
    'shopdesigner.html still loads shop-publisher.js');

// 4. Verify Shop Designer still loads shop-publications.js
assert(SHOP_DESIGNER_HTML.includes('shop-publications.js'),
    'shopdesigner.html still loads shop-publications.js');

// 5. Verify shared JS module still exists
const SHOP_PUBLISHER_JS = path.join(ROOT_DIR, 'dashboard', 'static', 'js', 'shop-publisher.js');
assert(fs.existsSync(SHOP_PUBLISHER_JS),
    'shop-publisher.js still exists for Shop Designer');

// 6. Verify NERO.shopPublisherPreview is defined
const SHOP_PUBLISHER_JS_CONTENT = fs.readFileSync(SHOP_PUBLISHER_JS, 'utf8');
assert(SHOP_PUBLISHER_JS_CONTENT.includes('NERO.shopPublisherPreview'),
    'NERO.shopPublisherPreview is defined in shop-publisher.js');

// 7. Verify shop-publications.js still exists
const SHOP_PUBLICATIONS_JS = path.join(ROOT_DIR, 'dashboard', 'static', 'js', 'shop-publications.js');
assert(fs.existsSync(SHOP_PUBLICATIONS_JS),
    'shop-publications.js still exists');

console.log('\n' + '='.repeat(60));
if (fail) {
    console.log(pass + '/' + (pass + fail) + ' checks — FAILURES:');
    failures.forEach(f => console.log('  - ' + f));
    process.exitCode = 1;
} else {
    console.log(pass + '/' + (pass + fail) + ' checks passed.');
}
