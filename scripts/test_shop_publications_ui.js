#!/usr/bin/env node
'use strict';
// Regression tests for Publication UI list gating, races, and in-flight locking.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
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
ids['sd-design-select'].value = 'A';
ids['sd-publication-channel'].value = '500';
doc.body.appendChild(root);
win.__CSRF_TOKEN__ = 'csrf';
const confirmations = [];
let confirmAccept = true;
win.confirm = message => { confirmations.push(message); return confirmAccept; };
const calls = [], deferred = [];
win.fetch = (url, init) => {
    calls.push({url, init});
    return new Promise(resolve => deferred.push({url, resolve: (body, ok = true, status = 200) =>
        resolve({ok, status, json: () => Promise.resolve(body)})}));
};
const sandbox = {window: win, document: doc, fetch: win.fetch, Promise, Number, String, Object, Array, Error, console};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../dashboard/static/js/shop-publications.js'), 'utf8'), sandbox);
function tick() { return new Promise(resolve => setTimeout(resolve, 0)); }
function assert(ok, label) { if (!ok) throw new Error('FAIL: ' + label); console.log('PASS', label); }
(async function () {
    // Initial selected Design starts with a pending list request and locked Publish.
    const reqA = deferred.shift();
    assert(ids['sd-publication-publish'].disabled, 'Publish disabled while initial Publication list loads');
    ids['sd-design-select'].value = 'B';
    ids['sd-design-select'].dispatch('change');
    const reqB = deferred.shift();
    assert(ids['sd-publication-publish'].disabled, 'Publish remains disabled while newly selected Design loads');
    reqB.resolve({success: true, publications: [{id: 22, channel_id: 222, status: 'published', message_id: 333}]});
    await tick();
    assert(!ids['sd-publication-publish'].disabled, 'Publish becomes available after successful list load');
    reqA.resolve({success: true, publications: [{id: 11, channel_id: 111, status: 'published', message_id: 222}]});
    await tick();
    const renderedRow = ids['sd-publication-list'].children[0];
    assert(renderedRow && renderedRow._text.indexOf('222') >= 0 && renderedRow._text.indexOf('111') < 0,
        'stale Design A response ignored after switching to B');

    ids['sd-design-select'].value = 'C';
    ids['sd-design-select'].dispatch('change');
    const reqC = deferred.shift();
    assert(ids['sd-publication-publish'].disabled, 'Publish disabled during subsequent Design list load');
    reqC.resolve({success: false, error: 'list unavailable'});
    await tick();
    assert(ids['sd-publication-publish'].disabled, 'Publish remains disabled after Publication-list failure');

    ids['sd-design-select'].value = 'D';
    ids['sd-design-select'].dispatch('change');
    const reqD = deferred.shift();
    reqD.resolve({success: true, publications: []});
    await tick();
    assert(!ids['sd-publication-publish'].disabled, 'successful list load restores Publish');

    // Only confirmed, lifecycle-actionable states receive Update/Unpublish.
    ids['sd-design-select'].value = 'E';
    ids['sd-design-select'].dispatch('change');
    const reqE = deferred.shift();
    reqE.resolve({success: true, publications: [
        {id: 31, channel_id: 501, status: 'pending', message_id: null},
        {id: 32, channel_id: 502, status: 'failed', message_id: null},
        {id: 33, channel_id: 503, status: 'updating', message_id: 603},
        {id: 34, channel_id: 504, status: 'replacement_sending', message_id: 604},
        {id: 35, channel_id: 505, status: 'unpublishing', message_id: 605},
        {id: 36, channel_id: 506, status: 'attention', message_id: null},
        {id: 37, channel_id: 507, status: 'published', message_id: 607},
        {id: 38, channel_id: 508, status: 'attention', message_id: 608},
        {id: 39, channel_id: 509, status: 'replacement_retry_authorized', message_id: 609},
        {id: 40, channel_id: 510, status: 'replacement_send_uncertain', message_id: 610},
    ]});
    await tick();
    const stateRows = ids['sd-publication-list'].children;
    assert(stateRows.length === 10, 'Publication statuses are rendered as separate rows');
    [0, 1, 2, 3, 4, 5].forEach(index => {
        assert(stateRows[index].querySelectorAll('button').length === 0 &&
            stateRows[index].textContent.indexOf('Not actionable:') !== -1,
            'message-less or locked state ' + index + ' has a clear non-actionable explanation');
    });
    assert(stateRows[6].querySelectorAll('button').length === 2 &&
        stateRows[7].querySelectorAll('button').length === 2,
        'published and actionable attention states offer Update and Unpublish');
    assert(stateRows[8].querySelectorAll('button').length === 1 &&
        stateRows[8].querySelectorAll('button')[0].textContent === 'Update',
        'replacement retry authorization permits only its deliberate Update');
    assert(stateRows[9].querySelectorAll('button').length === 1 &&
        stateRows[9].querySelectorAll('button')[0].textContent === 'Authorize replacement retry',
        'uncertain replacement exposes only the existing explicit authorization action');

    // One row operation disables its sibling, and a response for a previous
    // Design cannot overwrite the newly selected Design's status/list.
    const rowActions = stateRows[6].querySelectorAll('button');
    rowActions[0].click();
    const updateRequest = deferred.shift();
    assert(rowActions[0].disabled && rowActions[1].disabled,
        'starting Update disables both Update and Unpublish for that Publication');
    rowActions[1].click();
    assert(calls.filter(c => c.url.endsWith('/update') || c.url.endsWith('/publications/37')).length === 1,
        'the disabled sibling cannot send a conflicting Unpublish request');
    ids['sd-design-select'].value = 'F';
    ids['sd-design-select'].dispatch('change');
    const reqF = deferred.shift();
    reqF.resolve({success: true, publications: [{id: 51, channel_id: 511, status: 'published', message_id: 611}]});
    await tick();
    ids['sd-publication-status'].textContent = 'Current Design F status';
    updateRequest.resolve({success: true, publication: {id: 37, channel_id: 507, status: 'published', message_id: 607}});
    await tick();
    assert(ids['sd-publication-status'].textContent === 'Current Design F status',
        'a stale Update completion cannot overwrite the newly selected Design status');
    assert(ids['sd-publication-list'].children[0]._text.indexOf('511') !== -1,
        'a stale Update completion cannot replace the newly selected Design list');

    // Unpublish completions use the same Design/list-generation guard.
    const unpublishRowActions = ids['sd-publication-list'].children[0].querySelectorAll('button');
    const requestsBeforeUnpublish = calls.length;
    unpublishRowActions[1].click();
    const unpublishRequest = deferred.shift();
    assert(unpublishRowActions[0].disabled && unpublishRowActions[1].disabled,
        'starting Unpublish disables both Unpublish and Update for that Publication');
    unpublishRowActions[0].click();
    assert(calls.length === requestsBeforeUnpublish + 1,
        'the disabled sibling cannot send a conflicting Update request');
    ids['sd-design-select'].value = 'G';
    ids['sd-design-select'].dispatch('change');
    const reqG = deferred.shift();
    reqG.resolve({success: true, publications: [{id: 52, channel_id: 512, status: 'published', message_id: 612}]});
    await tick();
    ids['sd-publication-status'].textContent = 'Current Design G status';
    unpublishRequest.resolve({success: true, removed: true});
    await tick();
    assert(ids['sd-publication-status'].textContent === 'Current Design G status',
        'a stale Unpublish completion cannot overwrite the newly selected Design status');
    assert(ids['sd-publication-list'].children[0]._text.indexOf('512') !== -1,
        'a stale Unpublish completion cannot replace the newly selected Design list');

    // Return to a clean list before exercising Publish's in-flight lock.
    ids['sd-design-select'].value = 'D';
    ids['sd-design-select'].dispatch('change');
    const reqD2 = deferred.shift();
    reqD2.resolve({success: true, publications: []});
    await tick();

    // Keep the POST unresolved while a second click is attempted.
    ids['sd-publication-publish'].click();
    ids['sd-publication-publish'].click();
    const posts = calls.filter(c => c.url === '/api/shop-publisher/publications/publish');
    assert(posts.length === 1 && ids['sd-publication-publish'].disabled,
        'concurrent Publish click is locked while request is in flight');
    const post = deferred.shift();
    post.resolve({success: true, publication: {id: 23, status: 'published', message_id: 444}});
    await tick();
    const refreshedList = deferred.shift();
    refreshedList.resolve({success: true, publications: [{id: 23, channel_id: 500, status: 'published', message_id: 444}]});
    await tick(); await tick();
    assert(!ids['sd-publication-publish'].disabled,
        'Publish is restored after confirmed response and successful list refresh');

    // An uncertain result stays attached to its initiating Design/channel; it
    // must not annotate or prompt for a different selected Design.
    ids['sd-design-select'].value = 'U';
    ids['sd-design-select'].dispatch('change');
    const reqU = deferred.shift();
    reqU.resolve({success: true, publications: []});
    await tick();
    ids['sd-publication-publish'].click();
    const uncertainPost = deferred.shift();
    ids['sd-design-select'].value = 'V';
    ids['sd-design-select'].dispatch('change');
    const reqV = deferred.shift();
    reqV.resolve({success: true, publications: []});
    await tick();
    uncertainPost.resolve({success: false, uncertain: true,
        publication: {id: 60, channel_id: 500, status: 'pending'}}, true, 202);
    await tick(); await tick();
    assert(ids['sd-publication-status'].textContent === '',
        'uncertain Publish completion for U does not write status into selected Design V');
    const confirmCountBeforeV = confirmations.length;
    ids['sd-publication-publish'].click();
    assert(confirmations.length === confirmCountBeforeV,
        'U uncertainty does not trigger a duplicate warning for Design V');
    const postV = deferred.shift();
    postV.resolve({success: true, publication: {id: 61, channel_id: 500,
        status: 'published', message_id: 661}});
    await tick();
    const listV = deferred.shift();
    listV.resolve({success: true, publications: [{id: 61, channel_id: 500,
        status: 'published', message_id: 661}]});
    await tick(); await tick();

    // Returning to U keeps its warning scoped to the same channel only.
    ids['sd-design-select'].value = 'U';
    ids['sd-design-select'].dispatch('change');
    const reqU2 = deferred.shift();
    reqU2.resolve({success: true, publications: []});
    await tick();
    confirmAccept = false;
    const confirmCountBeforeU = confirmations.length;
    const publishCountBeforeU = calls.filter(c => c.url === '/api/shop-publisher/publications/publish').length;
    ids['sd-publication-publish'].click();
    assert(confirmations.length === confirmCountBeforeU + 1 &&
        calls.filter(c => c.url === '/api/shop-publisher/publications/publish').length === publishCountBeforeU,
        'the uncertain Publish warning remains scoped to Design U and its channel');
    console.log('All Publication UI regression checks passed.');
}()).catch(error => { console.error(error); process.exitCode = 1; });
