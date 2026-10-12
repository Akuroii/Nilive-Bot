#!/usr/bin/env node
'use strict';
// Re-check with Discord + Remove record only: gating, confirmation, requests, stale-response safety.
const fs = require('fs'), path = require('path'), vm = require('vm');
const { createDom, createWindow } = require('./support/dom_stub.js');
const dom = createDom(), win = createWindow(), doc = dom.document;
const root = doc.createElement('div'); root.setAttribute('data-page-module', 'shop-designer');
const ids = {};
[['sd-design-select', 'select'], ['sd-publication-channel', 'div'], ['sd-publication-publish', 'button'],
 ['sd-publication-recheck', 'button'], ['sd-publication-list', 'div'], ['sd-publication-status', 'div']].forEach(([id, tag]) => {
    const node = doc.createElement(tag); node.id = id; ids[id] = node; root.appendChild(node);
});
ids['sd-design-select'].value = '7';
doc.body.appendChild(root);
win.__CSRF_TOKEN__ = 'csrf';
const confirmations = []; let confirmAccept = true;
win.confirm = m => { confirmations.push(m); return confirmAccept; };
const calls = [], deferred = [];
win.fetch = (url, init) => { calls.push({url, init}); return new Promise(resolve => deferred.push({url, init,
    resolve: (body, ok = true, status = 200) => resolve({ok, status, json: () => Promise.resolve(body)})})); };
const sandbox = {window: win, document: doc, fetch: win.fetch, Promise, Number, String, Object, Array, Error, console};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../dashboard/static/js/shop-publications.js'), 'utf8'), sandbox);
const tick = () => new Promise(r => setTimeout(r, 0));
function assert(ok, label) { if (!ok) throw new Error('FAIL: ' + label); console.log('PASS', label); }
const labels = row => row.querySelectorAll('button').map(b => b.textContent);
const recheck = ids['sd-publication-recheck'], list = ids['sd-publication-list'], status = ids['sd-publication-status'];
const PUBS = [
    {id: 1, channel_id: 11, status: 'pending', message_id: null},
    {id: 2, channel_id: 12, status: 'attention', message_id: 902},
    {id: 3, channel_id: 13, status: 'published', message_id: 903},
    {id: 4, channel_id: 14, status: 'replacement_send_uncertain', message_id: 904},
    {id: 5, channel_id: 15, status: 'failed', message_id: null},
    {id: 6, channel_id: 16, status: 'unpublishing', message_id: 906},
];
(async function () {
    // list still loading -> Re-check disabled
    const first = deferred.shift();
    assert(recheck.disabled === true, 'Re-check is disabled while the publication list is loading');
    first.resolve({success: true, publications: PUBS}); await tick();
    assert(recheck.disabled === false, 'Re-check is enabled once the list for the selected design has loaded');

    const rows = list.children;
    assert(labels(rows[0]).join() === 'Remove record only', 'pending: only Remove record only');
    assert(labels(rows[1]).join() === 'Update,Unpublish,Remove record only', 'attention with message: Update, Unpublish, Remove record only');
    assert(labels(rows[2]).join() === 'Update,Unpublish', 'published: NO record-only removal (Unpublish must be used)');
    assert(labels(rows[3]).join() === 'Authorize replacement retry,Remove record only', 'uncertain replacement: authorize + record-only');
    assert(labels(rows[4]).join() === 'Unpublish', 'failed orphan: Unpublish only');
    assert(labels(rows[5]).length === 0, 'in-flight row: no actions at all');

    // --- Remove record only: declined -> nothing sent
    confirmAccept = false; let before = calls.length;
    rows[0].querySelectorAll('button')[0].click();
    assert(calls.length === before, 'declining the record-only confirmation sends no request');
    const msg = confirmations[confirmations.length - 1];
    assert(/database only/i.test(msg) && /Discord is NOT contacted/.test(msg) && /delete it manually/.test(msg),
        'confirmation says database-only, Discord not contacted, message may need manual deletion');
    // accepted -> POST /forget {confirm:true}, then reload
    confirmAccept = true;
    rows[0].querySelectorAll('button')[0].click();
    const f = deferred.shift();
    assert(f.url === '/api/shop-publisher/publications/1/forget' && f.init.method === 'POST' &&
        JSON.parse(f.init.body).confirm === true, 'POST /publications/1/forget with {confirm:true}');
    assert(rows[0].querySelectorAll('button')[0].disabled === true, 'button disables while the request runs');
    f.resolve({success: true, discord_untouched: true}); await tick();
    assert(/Discord was not contacted/.test(status.textContent), 'status states Discord was not contacted');
    const reload = deferred.shift(); assert(/\/designs\/7\/publications$/.test(reload.url), 'list reloads after removal');
    reload.resolve({success: true, publications: PUBS.slice(1)}); await tick();

    // --- failure keeps row and re-enables the button
    list.children[0].querySelectorAll('button')[2].click();           // attention row: Remove record only
    const bad = deferred.shift();
    bad.resolve({success: false, error: 'This Publication is not in a state that allows record-only removal.'}, false, 409); await tick();
    assert(/not in a state/.test(status.textContent), 'server refusal is shown, not swallowed');
    assert(list.children[0].querySelectorAll('button')[2].disabled === false, 'button re-enables after a refusal');

    // --- Re-check
    recheck.click();
    const rc = deferred.shift();
    assert(rc.url === '/api/shop-publisher/designs/7/publications/recheck' && rc.init.method === 'POST', 'POST /designs/7/publications/recheck');
    assert(recheck.disabled === true && /Checking Discord/.test(status.textContent), 'Re-check disables itself and shows progress');
    recheck.click(); assert(deferred.length === 0, 'a second click while running sends nothing');
    rc.resolve({success: true, removed: 2, still_present: 1, unverified: 1, skipped: 0}); await tick();
    assert(/2 stale record\(s\) cleared/.test(status.textContent) && /1 still on Discord/.test(status.textContent) &&
        /1 could not be verified and were left untouched/.test(status.textContent), 'summary reports cleared / present / unverified honestly');
    deferred.shift().resolve({success: true, publications: []}); await tick();
    assert(recheck.disabled === false, 'Re-check re-enables after completion');

    // --- stale Re-check response must not overwrite a newer selection
    recheck.click(); const stale = deferred.shift();
    ids['sd-design-select'].value = '8'; ids['sd-design-select'].dispatch('change');
    const other = deferred.shift(); other.resolve({success: true, publications: []}); await tick();
    status.textContent = 'design 8 status';
    stale.resolve({success: true, removed: 5, still_present: 0, unverified: 0}); await tick();
    assert(status.textContent === 'design 8 status', 'a stale Re-check result cannot overwrite another design\'s status');
    assert(recheck.disabled === false, 'Re-check is usable again for the newly selected design');

    // --- blocked Update (HTTP 400) surfaces every validation problem, and the row stays actionable
    ids['sd-design-select'].value = '9'; ids['sd-design-select'].dispatch('change');
    deferred.shift().resolve({success: true, publications: [{id: 20, channel_id: 21, status: 'published', message_id: 999}]}); await tick();
    list.children[0].querySelectorAll('button')[0].click();                    // Update
    deferred.shift().resolve({success: false, code: 'publication_validation_failed', error: 'No product 5 in this guild.',
        problems: [{message: 'No product 5 in this guild.'}, {message: 'No product 6 in this guild.'}]}, false, 400); await tick();
    assert(/No product 5 in this guild\./.test(status.textContent) && /\+1 more: No product 6 in this guild\./.test(status.textContent),
        'a blocked Update shows the first problem and the remaining ones');
    deferred.shift().resolve({success: true, publications: [{id: 20, channel_id: 21, status: 'published', message_id: 999}]}); await tick();
    assert(labels(list.children[0]).join() === 'Update,Unpublish', 'after a blocked Update the row is untouched and Unpublish stays available');

    // --- Re-check disabled when nothing selected
    ids['sd-design-select'].value = ''; ids['sd-design-select'].dispatch('change');
    assert(recheck.disabled === true, 'Re-check is disabled with no design selected');
    console.log('All publication recovery UI checks passed.');
})().catch(e => { console.error(e.message); process.exit(1); });
