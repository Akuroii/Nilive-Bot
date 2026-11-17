#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Member wallet edits — what the two dashboard forms really send

   POST /api/edit-member overwrites a member's coins / diamonds with the
   numbers the form holds. The server therefore REQUIRES, for every
   currency it is asked to write, the value the form was opened with
   (`expected_coins` / `expected_diamonds`) and refuses a stale one.

   This runs the forms' own JavaScript — the edit modal in dashboard.js
   and saveProfile() in member_profile.html — against a stub DOM and
   records the request body. It locks down:
     * a currency the admin did not change is not sent at all, so an
       XP-only edit can never touch (or be blocked by) the wallet;
     * a currency the admin changed is sent together with its snapshot;
     * after a successful save the snapshot moves to the saved value.

   Run:  node scripts/test_member_wallet_snapshot.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : ''));
           console.log('  FAIL', name, extra || ''); }
}
function same(actual, expected, name) {
    assert(JSON.stringify(actual) === JSON.stringify(expected), name,
           'got ' + JSON.stringify(actual) + ' want ' + JSON.stringify(expected));
}
function section(t) { console.log('\n== ' + t + ' =='); }

const ROOT = path.join(__dirname, '..');
function slice(source, from, to, label) {
    const a = source.indexOf(from);
    const b = source.indexOf(to, a + from.length);
    if (a < 0 || b < 0) throw new Error('cannot find ' + label);
    return source.slice(a, b);
}
function dom(values) {
    const nodes = {};
    for (const [id, value] of Object.entries(values)) {
        nodes[id] = { value: String(value), defaultValue: String(value),
                      style: {}, textContent: '', innerHTML: '' };
    }
    return {
        nodes,
        getElementById(id) {
            if (!nodes[id]) nodes[id] = { value: '', defaultValue: '', style: {},
                                           textContent: '', innerHTML: '' };
            return nodes[id];
        },
        querySelector() { return { disabled: false }; },
    };
}

// ── the edit modal (dashboard.js) ─────────────────────────────────────
const MODAL = slice(
    fs.readFileSync(path.join(ROOT, 'dashboard', 'static', 'js', 'dashboard.js'), 'utf8'),
    '// ── EDIT MEMBER MODAL', '// ── COPY TO CLIPBOARD', 'the edit modal');

async function modalSave(openWith, typed) {
    const sent = [];
    const document = dom({ 'edit-xp': 0, 'edit-coins': 0, 'edit-uid-label': '',
                           'edit-modal': '' });
    const sandbox = {
        document, console,
        ajaxSave: async (url, body) => { sent.push({ url, body }); return { success: false }; },
        location: { reload() {} }, setTimeout,
    };
    vm.createContext(sandbox);
    vm.runInContext(MODAL + '\nthis.openEditModal = openEditModal;' +
                    '\nthis.saveEditModal = saveEditModal;', sandbox);
    sandbox.openEditModal(...openWith);
    for (const [id, value] of Object.entries(typed || {})) document.nodes[id].value = String(value);
    await sandbox.saveEditModal();
    return sent;
}

// ── the profile page (member_profile.html) ────────────────────────────
const PROFILE = slice(
    fs.readFileSync(path.join(ROOT, 'dashboard', 'templates', 'general',
                              'member_profile.html'), 'utf8'),
    'async function saveProfile() {', '\n}\n', 'saveProfile()') + '\n}\n';

function profilePage(loaded, answer) {
    const sent = [];
    const document = dom({ 'edit-xp': loaded.xp, 'edit-coins': loaded.coins,
                           'edit-diamonds': loaded.diamonds, 'profile-status': '' });
    const sandbox = {
        document, console,
        fetch: async (url, options) => {
            sent.push({ url, body: JSON.parse(options.body) });
            return { json: async () => (answer || { success: true }) };
        },
        checkIconHtml: () => '', showToast() {},
        setTimeout() {}, location: { reload() {} },
    };
    vm.createContext(sandbox);
    vm.runInContext(PROFILE + '\nthis.saveProfile = saveProfile;', sandbox);
    return { sent, document, save: () => sandbox.saveProfile() };
}

(async () => {
    section('edit modal');
    let sent = await modalSave(['77', 500, 1000], { 'edit-xp': 900 });
    same(sent.length, 1, 'one request');
    same(sent[0].url, '/api/edit-member', 'posts to the member edit route');
    same(sent[0].body, { user_id: '77', xp: 900 },
         'XP-only edit sends no wallet field');

    sent = await modalSave(['77', 500, 1000], { 'edit-coins': 1500 });
    same(sent[0].body, { user_id: '77', xp: 500, coins: 1500, expected_coins: 1000 },
         'changed coins are sent with the balance the modal opened with');

    sent = await modalSave(['77', 500, 0], { 'edit-coins': 25 });
    same(sent[0].body, { user_id: '77', xp: 500, coins: 25, expected_coins: 0 },
         'a zero opening balance is still sent as the snapshot');

    sent = await modalSave(['77', 500, 1000], { 'edit-coins': 0 });
    same(sent[0].body, { user_id: '77', xp: 500, coins: 0, expected_coins: 1000 },
         'setting coins to 0 is a write and carries the snapshot');

    sent = await modalSave(['77', 500, 1000], { 'edit-coins': '' });
    same(sent[0].body, { user_id: '77', xp: 500 },
         'an emptied coins field writes nothing');

    section('member profile page');
    let page = profilePage({ xp: 500, coins: 1000, diamonds: 138 });
    page.document.nodes['edit-xp'].value = '900';
    await page.save();
    same(Object.keys(page.sent[0].body).sort(), ['user_id', 'xp'],
         'XP-only edit sends no wallet field');
    same(page.sent[0].body.xp, 900, 'the edited XP is sent');

    page = profilePage({ xp: 500, coins: 1000, diamonds: 138 });
    page.document.nodes['edit-coins'].value = '1500';
    await page.save();
    same([page.sent[0].body.coins, page.sent[0].body.expected_coins], [1500, 1000],
         'changed coins carry the loaded balance');
    assert(!('diamonds' in page.sent[0].body) && !('expected_diamonds' in page.sent[0].body),
           'untouched diamonds are not sent');

    page = profilePage({ xp: 500, coins: 1000, diamonds: 138 });
    page.document.nodes['edit-diamonds'].value = '7';
    await page.save();
    same([page.sent[0].body.diamonds, page.sent[0].body.expected_diamonds], [7, 138],
         'changed diamonds carry the loaded diamonds');
    assert(!('coins' in page.sent[0].body) && !('expected_coins' in page.sent[0].body),
           'untouched coins are not sent');

    page = profilePage({ xp: 500, coins: 1000, diamonds: 138 });
    page.document.nodes['edit-coins'].value = '1';
    page.document.nodes['edit-diamonds'].value = '2';
    await page.save();
    same([page.sent[0].body.coins, page.sent[0].body.expected_coins,
          page.sent[0].body.diamonds, page.sent[0].body.expected_diamonds],
         [1, 1000, 2, 138], 'both currencies carry their own snapshot');
    // saved: the page now knows the new wallet
    page.document.nodes['edit-xp'].value = '901';
    await page.save();
    same(Object.keys(page.sent[1].body).sort(), ['user_id', 'xp'],
         'after a successful save the saved values are the new snapshot');
    page.document.nodes['edit-coins'].value = '5';
    await page.save();
    same([page.sent[2].body.coins, page.sent[2].body.expected_coins], [5, 1],
         'a later change is checked against the saved balance');

    page = profilePage({ xp: 500, coins: 1000, diamonds: 138 },
                       { success: false, stale: true, error: 'wallet changed' });
    page.document.nodes['edit-coins'].value = '1500';
    await page.save();
    await page.save();
    same([page.sent[1].body.coins, page.sent[1].body.expected_coins], [1500, 1000],
         'a refused save keeps the original snapshot (no silent overwrite on retry)');
    same(page.document.nodes['profile-status'].textContent, 'wallet changed',
         'the refusal is shown to the admin');

    console.log(`\n${pass} passed, ${fail} failed`);
    if (fail) { failures.forEach(f => console.log('  - ' + f)); process.exit(1); }
})().catch(err => { console.error(err); process.exit(1); });
