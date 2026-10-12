#!/usr/bin/env node
'use strict';
// A definitively failed, message-less Publication offers Unpublish (and nothing
// else). pending / message-bearing failed / published rows keep their old rules.
const fs = require('fs'), path = require('path'), vm = require('vm');
const { createDom, createWindow } = require('./support/dom_stub.js');
const dom = createDom(), win = createWindow(), doc = dom.document;
const root = doc.createElement('div');
root.setAttribute('data-page-module', 'shop-designer');
const ids = {};
['sd-design-select', 'sd-publication-channel', 'sd-publication-publish',
 'sd-publication-list', 'sd-publication-status'].forEach(id => {
    const node = doc.createElement(id === 'sd-design-select' ? 'select' : 'div');
    node.id = id; ids[id] = node; root.appendChild(node);
});
ids['sd-design-select'].value = '7';
ids['sd-publication-channel'].value = '1088248319982006322';
doc.body.appendChild(root);
win.__CSRF_TOKEN__ = 'csrf';
const confirmations = []; let confirmAccept = true;
win.confirm = m => { confirmations.push(m); return confirmAccept; };
const calls = [], deferred = [];
win.fetch = (url, init) => { calls.push({url, init}); return new Promise(resolve => deferred.push({url,
    resolve: (body, ok = true, status = 200) => resolve({ok, status, json: () => Promise.resolve(body)})})); };
const sandbox = {window: win, document: doc, fetch: win.fetch, Promise, Number, String, Object, Array, Error, console};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../dashboard/static/js/shop-publications.js'), 'utf8'), sandbox);
const tick = () => new Promise(r => setTimeout(r, 0));
function assert(ok, label) { if (!ok) throw new Error('FAIL: ' + label); console.log('PASS', label); }
const labels = row => row.querySelectorAll('button').map(b => b.textContent);
(async function () {
    deferred.shift().resolve({success: true, publications: [
        {id: 1, channel_id: 11, status: 'failed', message_id: null},     // orphan -> Unpublish only
        {id: 2, channel_id: 12, status: 'pending', message_id: null},    // uncertain -> protected
        {id: 3, channel_id: 13, status: 'failed', message_id: 903},      // failed WITH message -> locked
        {id: 4, channel_id: 14, status: 'published', message_id: 904},   // unchanged
        {id: 5, channel_id: 15, status: 'attention', message_id: null},  // unchanged (locked)
    ]});
    await tick();
    const rows = ids['sd-publication-list'].children;
    assert(rows.length === 5, 'all five rows render');
    assert(labels(rows[0]).join() === 'Unpublish', 'failed + no message ID offers exactly Unpublish (no Update)');
    assert(labels(rows[1]).join() === 'Remove record only' && rows[1].textContent.indexOf('Not actionable:') !== -1,
        'pending row offers no Update/Unpublish (only the explicit record-only removal), clear explanation');
    assert(labels(rows[2]).length === 0 && rows[2].textContent.indexOf('Not actionable:') !== -1,
        'failed row that carries a message ID stays locked');
    assert(labels(rows[3]).join() === 'Update,Unpublish', 'published row still offers Update and Unpublish');
    assert(labels(rows[4]).join() === 'Remove record only' && rows[4].textContent.indexOf('Not actionable:') !== -1,
        'message-less attention row offers no Update/Unpublish (only the explicit record-only removal)');

    // Declining the confirmation sends nothing.
    confirmAccept = false;
    const before = calls.length;
    rows[0].querySelectorAll('button')[0].click();
    assert(calls.length === before && confirmations.length === 1, 'declined confirmation sends no request');
    assert(/no Discord message exists/.test(confirmations[0]) && !/Delete this Discord message/.test(confirmations[0]),
        'failed-row confirmation says no Discord message exists (not "Delete this Discord message")');

    // Accepting sends DELETE for exactly that Publication, then reloads the list.
    confirmAccept = true;
    rows[0].querySelectorAll('button')[0].click();
    const del = deferred.shift();
    const call = calls[calls.length - 1];
    assert(call.url === '/api/shop-publisher/publications/1' && call.init.method === 'DELETE',
        'Unpublish issues DELETE /api/shop-publisher/publications/1');
    del.resolve({success: true, already_missing: false});
    await tick();
    const reload = deferred.shift();
    assert(/\/designs\/7\/publications$/.test(reload.url), 'list reloads after the failed row is cleared');
    reload.resolve({success: true, publications: []});
    await tick();
    assert(ids['sd-publication-list'].children.length === 0, 'cleared row disappears from the list');
    console.log('All failed-publication UI checks passed.');
})().catch(e => { console.error(e.message); process.exit(1); });
