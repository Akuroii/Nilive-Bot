/* Real DOM interaction test for the Shop Designer's catalog-wide icon control.
 *
 * Why this exists: asserting that a template string contains a button id or that
 * the script mentions an endpoint does NOT prove a user can click the control and
 * that it reaches the API. This harness renders the REAL template markup, loads
 * the REAL shop-designer.js, mounts it through the REAL page lifecycle, clicks the
 * button with a synthesised event, and asserts on the outgoing request.
 *
 * Run: node scripts/test_shop_designer_ui.js
 * Exit code 0 = every interaction assertion held.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
/* jsdom is not vendored in this repository (it is a dev-only harness dep), so
 * resolve it from the usual places and fail with an actionable message. */
function loadJsdom() {
    const candidates = [
        process.env.JSDOM_PATH,
        'jsdom',
        path.join(ROOT, 'node_modules/jsdom'),
        '/tmp/jsdomrig/node_modules/jsdom',
    ].filter(Boolean);
    for (const candidate of candidates) {
        try { return require(candidate); } catch (e) { /* try the next one */ }
    }
    console.error('jsdom is required for this test. Install it with:\n' +
                  '  npm install --no-save jsdom\n' +
                  'or point JSDOM_PATH at an existing installation.');
    process.exit(2);
}
const { JSDOM } = loadJsdom();

const TEMPLATE = fs.readFileSync(
    path.join(ROOT, 'dashboard/templates/manage/shopdesigner.html'), 'utf8');
const LIFECYCLE = fs.readFileSync(
    path.join(ROOT, 'dashboard/static/js/nav-lifecycle.js'), 'utf8');
const DESIGNER = fs.readFileSync(
    path.join(ROOT, 'dashboard/static/js/shop-designer.js'), 'utf8');

let failures = 0;
let checks = 0;
function check(condition, message) {
    checks += 1;
    if (!condition) {
        failures += 1;
        console.error('  FAIL: ' + message);
    }
}

/* Extract the Shop Designer's page-root block from the Jinja template.
 * The template extends a base layout, so we take the block that carries
 * data-page-module="shop-designer" and drop Jinja control flow. */
function pageMarkup() {
    const start = TEMPLATE.indexOf('data-page-module="shop-designer"');
    if (start < 0) throw new Error('the template has no shop-designer page root');
    const open = TEMPLATE.lastIndexOf('<div', start);
    // Balance <div> tags from the root so we capture exactly this page.
    const tagRe = /<div\b[^>]*>|<\/div>/g;
    tagRe.lastIndex = open;
    let depth = 0;
    let end = -1;
    let match;
    while ((match = tagRe.exec(TEMPLATE)) !== null) {
        depth += match[0].startsWith('</') ? -1 : 1;
        if (depth === 0) { end = tagRe.lastIndex; break; }
    }
    if (end < 0) throw new Error('unbalanced page root markup');
    return TEMPLATE.slice(open, end);
}

/* Minimal Jinja stripping: the icon panel has no conditionals, but the page
 * body may. Removing {{...}} / {%...%} keeps the static structure intact. */
function stripJinja(markup) {
    return markup
        .replace(/\{%-?\s*if[\s\S]*?\{%-?\s*endif\s*-?%\}/g, '')
        .replace(/\{\{[\s\S]*?\}\}/g, 'placeholder')
        .replace(/\{%[\s\S]*?%\}/g, '');
}

const requests = [];

const dom = new JSDOM(
    '<!doctype html><html><body>' + stripJinja(pageMarkup()) + '</body></html>',
    { runScripts: 'outside-only', url: 'https://dashboard.example.test/manage/shopdesigner' });

const { window } = dom;
const { document } = window;

// A fetch stub that records every call and answers with realistic payloads.
window.fetch = function (url, init) {
    requests.push({ url: String(url), method: (init && init.method) || 'GET',
                    body: init && init.body ? String(init.body) : null });
    const payload = String(url).indexOf('/emojis/report') >= 0
        ? { success: true, eligible_products: 7, already_ready: 4,
            unprepared_products: 3, distinct_urls: 7, assets_to_attempt_max: 3,
            shared_url_groups: [], content_reused_groups: [],
            invalid_source_products: [{ product_id: 9, name: 'Bad URL',
                                        icon_url: 'http://x/y.png',
                                        reason: 'Only HTTPS image URLs are supported.' }],
            quota: { used: 4, limit: 100 }, quota_limited: false,
            uncertain: 0, failed_or_missing: 1,
            parked: [{ product_id: 5, item_name: 'Dead Image', attempts: 4,
                       last_error: 'source_http_error: 404' }],
            queue: { total: 2, parked: 1, deferred: 1, retrying_backoff: 1 } }
        : { success: true, queued: 3, reactivated: 0, queue: { total: 3 } };
    return Promise.resolve({ ok: true, status: 200,
                             json: function () { return Promise.resolve(payload); } });
};
window.confirm = function () { return true; };
window.requestAnimationFrame = function (fn) { return setTimeout(fn, 0); };

// Load the real lifecycle + page module through the window's own eval.
window.eval(LIFECYCLE);
window.eval(DESIGNER);

const NERO = window.NERO;
check(NERO && typeof NERO.lifecycle === 'object',
      'nav-lifecycle.js exposes NERO.lifecycle');
check(NERO.debug && NERO.debug.report && NERO.debug.report().debug !== undefined,
      'lifecycle loaded a real page registry');

const root = document.querySelector('[data-page-module="shop-designer"]');
check(root !== null, 'the rendered DOM contains the shop-designer page root');

function flush() {
    return new Promise(function (resolve) { setTimeout(resolve, 60); });
}

(async function run() {
    // Mount through the same entry point the real dashboard uses.
    await NERO.lifecycle.mount(document);
    await flush();

    // ── the control exists as a real, enabled, clickable element ──────────
    const backfill = document.getElementById('sd-catalog-emoji-backfill');
    check(backfill !== null, 'the "Prepare all Shop product icons" button is in the DOM');
    check(backfill && backfill.tagName === 'BUTTON', 'the control is a real <button>');
    check(backfill && !backfill.disabled,
          'the control is enabled even though the roster is empty');
    check(backfill && /Prepare all Shop product icons/.test(backfill.textContent),
          'the control is labelled for the whole catalog');

    const status = document.getElementById('sd-catalog-emoji-status');
    const reportBox = document.getElementById('sd-catalog-emoji-report');
    check(status !== null && reportBox !== null, 'progress elements exist');

    // Mounting must have fetched the report (the UI shows progress on open).
    const initial = requests.filter(function (r) {
        return r.url.indexOf('/emojis/report') >= 0;
    });
    check(initial.length >= 1, 'opening the page reads the progress report');
    check(initial.every(function (r) { return r.method === 'GET'; }),
          'reading progress is a GET (read-only)');
    check((reportBox.textContent || '').indexOf('Already prepared') >= 0,
          'the report is actually rendered, not just requested');
    check((reportBox.textContent || '').indexOf('3') >= 0,
          'the unprepared count reaches the DOM');
    check((reportBox.textContent || '').indexOf('Dead Image') >= 0,
          'parked products and their failure reasons are shown');
    check((status.textContent || '').indexOf('3 product(s) still need an icon') >= 0,
          'the status line states the outstanding work');

    // ── CLICK the control for real ────────────────────────────────────────
    const before = requests.length;
    backfill.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await flush();

    const afterClick = requests.slice(before);
    const posts = afterClick.filter(function (r) {
        return r.url.indexOf('/api/shop-publisher/emojis/backfill') >= 0;
    });
    check(posts.length === 1, 'one click issues exactly one backfill request');
    check(posts.length === 1 && posts[0].method === 'POST',
          'the request is a POST');
    check(posts.length === 1 && /retry_parked/.test(posts[0].body || ''),
          'the request body carries the retry_parked switch');
    check(posts.length === 1 && /false/.test(posts[0].body || ''),
          'retry_parked defaults to false when the box is unticked');
    check(posts.length === 1 && !/product_ids/.test(posts[0].body || ''),
          'the catalog action never sends a product list from the roster');

    // The click must refresh progress afterwards (the user sees the result).
    const refreshes = afterClick.filter(function (r) {
        return r.url.indexOf('/emojis/report') >= 0;
    });
    check(refreshes.length >= 1, 'the click refreshes the progress report');
    check((status.textContent || '').indexOf('Queued 3 product(s)') >= 0,
          'the queued count is reported back to the user: ' + status.textContent);

    // ── ticking the parked-retry box changes the request ──────────────────
    const retryBox = document.getElementById('sd-catalog-emoji-retry-parked');
    check(retryBox !== null, 'the parked-retry switch exists');
    retryBox.checked = true;
    const before2 = requests.length;
    backfill.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await flush();
    const posts2 = requests.slice(before2).filter(function (r) {
        return r.url.indexOf('/emojis/backfill') >= 0;
    });
    check(posts2.length === 1 && /retry_parked":true/.test(posts2[0].body || ''),
          'the retry_parked switch is honoured in the request body');

    // ── the refresh button works and stays read-only ──────────────────────
    const refreshBtn = document.getElementById('sd-catalog-emoji-refresh');
    check(refreshBtn !== null, 'the refresh control exists');
    const before3 = requests.length;
    refreshBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await flush();
    const gets = requests.slice(before3);
    check(gets.length >= 1 && gets.every(function (r) { return r.method === 'GET'; }),
          'Refresh only issues GETs');

    // ── a failing API is surfaced, never swallowed ────────────────────────
    let failingCalls = 0;
    window.fetch = function () {
        failingCalls += 1;
        return Promise.resolve({ ok: false, status: 500,
                                 json: function () {
                                     return Promise.resolve({ success: false,
                                                              error: 'boom from server' });
                                 } });
    };
    const before4 = requests.length;
    backfill.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await flush();
    check(failingCalls >= 1, 'the control still calls the API when it will fail');
    check(/boom from server/.test(status.textContent || ''),
          'a server error is shown to the owner: ' + status.textContent);
    check(!backfill.disabled, 'the control is re-enabled after a failure');

    console.log('  ' + checks + ' interaction checks, ' + failures + ' failure(s)');
    if (failures) {
        console.error('UI INTERACTION TEST FAILED');
        process.exit(1);
    }
    console.log('UI INTERACTION TEST PASSED');
    process.exit(0);
})().catch(function (error) {
    console.error('UI INTERACTION TEST CRASHED: ' + (error && error.stack || error));
    process.exit(1);
});
