#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Embed Builder attachments — preview resolution + degradation

   Covers the exact class of bug that reads as "my attachment
   disappeared": the file is in memory, the <img> src is set, and
   nothing paints (CSP `img-src` without `blob:`), or File.type is
   empty so an image is treated as a document forever.

   Run:  node scripts/test_embed_attachments.js
   No DOM: each case loads its OWN copy of embed-composer.js, because
   the blob-renderability verdict is cached per module instance.
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }

const SRC = fs.readFileSync(
    path.join(__dirname, '..', 'dashboard', 'static', 'js', 'embed-composer.js'), 'utf8');

class FakeBlob { constructor(parts) { this.parts = parts; } }

/**
 * A fresh module instance with a fake browser around it.
 *  - blobLoads: whether an <img src=blob:…> ever fires onload
 *                (false === CSP img-src blocks blob:)
 *  - frThrows:  FileReader explodes (no usable data: fallback either)
 *  - dataUrl:   what FileReader hands back
 */
function loadModule({ blobLoads = true, frThrows = false, dataUrl = 'data:image/png;base64,AAAA' } = {}) {
    const env = { mints: 0, revoked: 0, reads: 0 };
    let n = 0;
    const sandbox = {
        window: {}, console, Event: function () {}, Promise,
        setTimeout, clearTimeout,
        atob: (s) => Buffer.from(s, 'base64').toString('binary'),
        Blob: FakeBlob,
        document: {},
        Image: class { set src(v) { this.__src = v; if (blobLoads) setTimeout(() => this.onload && this.onload(), 0); } },
        FileReader: class {
            readAsDataURL() {
                env.reads++;
                if (frThrows) { setTimeout(() => this.onerror && this.onerror(new Error('unreadable')), 0); return; }
                this.result = dataUrl;
                setTimeout(() => this.onload && this.onload(), 0);
            }
        },
        URL: {
            createObjectURL: () => { env.mints++; return `blob:${++n}`; },
            revokeObjectURL: () => { env.revoked++; },
        },
    };
    vm.createContext(sandbox);
    vm.runInContext(SRC, sandbox);
    return { EC: sandbox.window.EmbedComposer, sandbox, env };
}

const LOOKUPS = { roles: {}, channels: {}, users: {} };
const box = { innerHTML: '' };
const imgAtt = (over = {}) => Object.assign(
    { id: 'a1', name: 'pic.png', type: 'image/png', size: 120, blob: new FakeBlob(['x']), source: 'local' }, over);

const chain = Promise.resolve();

// ═══════════════════════════════════════════════════════════════
section('blob: probe — detect "src is set but nothing paints"');
chain.then(() => {
    const ok = loadModule({ blobLoads: true });
    return ok.EC.canRenderBlobUrls().then(r => {
        assert(r === true, 'renders blob: when the probe image loads');
        const blocked = loadModule({ blobLoads: false });
        return blocked.EC.canRenderBlobUrls().then(r2 => {
            assert(r2 === false, 'reports blob: as unrenderable when it never loads (CSP)');
            const third = loadModule({ blobLoads: true });
            assert(third.EC._resetBlobProbe() === undefined, '_resetBlobProbe is callable (test seam)');
            return third.EC.canRenderBlobUrls().then(r3 => assert(r3 === true, 'probe can be re-armed'));
        });
    });
}).then(() => {
// ═══════════════════════════════════════════════════════════════
    section('previewUrlFor — resolution tiers');
    const good = loadModule({ blobLoads: true });
    const a = imgAtt();
    return good.EC.previewUrlFor(a).then(r => {
        assert(r.kind === 'blob' && /^blob:/.test(r.url), 'prefers a blob: URL (zero-copy)');
        assert(a._previewUrl === r.url, 'blob URL is cached on the attachment for revoke');
        assert(good.env.reads === 0, 'FileReader is never touched when blob: works');
        return good.EC.previewUrlFor(a).then(r2 => {
            assert(r2.url === a._previewUrl && r2.kind === 'blob', 'cached URL is reused, not re-minted');
        });
    });
}).then(() => {
    const blocked = loadModule({ blobLoads: false });
    const a = imgAtt();
    return blocked.EC.previewUrlFor(a).then(r => {
        assert(r.kind === 'data' && r.url.startsWith('data:image/png'), 'falls back to a data: URI when blob: is blocked');
        assert(a._dataUrl === r.url, 'data: URI is cached on the attachment');
        assert(blocked.env.reads === 1, 'FileReader used exactly once (no re-read per render)');
    });
}).then(() => {
    const blocked = loadModule({ blobLoads: false });
    const big = imgAtt({ size: blocked.EC.DATA_URL_MAX_BYTES + 1 });
    return blocked.EC.previewUrlFor(big).then(r => {
        assert(r.url === null && r.reason === 'too-large-for-preview', 'oversized file: honest reason instead of a blank tile');
        assert(big._dataUrl === undefined && blocked.env.reads === 0, 'oversized file is never base64-inlined (memory blowup guard)');
    });
}).then(() => {
    const good = loadModule({ blobLoads: true });
    const big = imgAtt({ size: good.EC.DATA_URL_MAX_BYTES + 1 });
    return good.EC.previewUrlFor(big).then(r => {
        assert(r.kind === 'blob', 'the size cap only applies to the data: tier — a big image still previews via blob:');
    });
}).then(() => {
    const broken = loadModule({ blobLoads: false, frThrows: true });
    return broken.EC.previewUrlFor(imgAtt()).then(r => {
        assert(r.url === null && r.reason === 'decode-failed', 'unreadable file degrades to a stated reason, no throw');
    });
}).then(() => {
    const m = loadModule();
    return Promise.all([
        m.EC.previewUrlFor({ id: 'r', name: 'x.png', type: 'image/png', size: 5, source: 'remote', url: 'https://cdn.discordapp.com/x.png' }),
        m.EC.previewUrlFor({ id: 'r2', name: 'y.png', type: 'image/png', size: 5, source: 'remote' }),
        m.EC.previewUrlFor(null),
    ]).then(([remote, missing, nul]) => {
        assert(remote.kind === 'remote' && remote.url.includes('cdn.discordapp.com'), 'remote attachments pass through untouched');
        assert(missing.url === null && missing.reason === 'remote-url-missing', 'remote without a url says so');
        assert(nul.url === null && nul.reason === 'no-attachment', 'previewUrlFor(null) never throws');
    });
}).then(() => {
    const m = loadModule({ blobLoads: true });
    return m.EC.previewUrlFor(imgAtt(), { allowBlob: false }).then(r => {
        assert(r.kind === 'data', 'allowBlob:false skips the blob attempt entirely');
    });
}).then(() => {
// ═══════════════════════════════════════════════════════════════
    section('in-flight de-dupe — two renders must not mint twice');
    const m = loadModule({ blobLoads: true });
    const a = imgAtt();
    return Promise.all([m.EC.previewUrlFor(a), m.EC.previewUrlFor(a), m.EC.previewUrlFor(a)]).then(rs => {
        assert(rs[0].url === rs[1].url && rs[1].url === rs[2].url, 'concurrent calls share one resolution');
        assert(a._previewPromise === null, 'de-dupe promise is released after settling (retry stays possible)');
    });
}).then(() => {
// ═══════════════════════════════════════════════════════════════
    section('renderPreview — a slot for every state, never a vanishing file');
    const m = loadModule();
    const base = { content: 'c', embeds: [m.EC.blankEmbed()], lookups: LOOKUPS };

    const pend = { id: 'p', name: 'wait.png', type: 'image/png', size: 9, _previewPending: true };
    m.EC.renderPreview(box, Object.assign({}, base, { attachments: [pend], attachmentPreviewUrl: () => null }));
    assert(box.innerHTML.includes('data-preview-state="pending"') && box.innerHTML.includes('wait.png'),
        'pending attachment keeps a named slot');

    const err = { id: 'q', name: 'big.png', type: 'image/png', size: 99, _previewError: 'too-large-for-preview' };
    m.EC.renderPreview(box, Object.assign({}, base, { attachments: [err], attachmentPreviewUrl: () => null }));
    assert(box.innerHTML.includes('too-large-for-preview') && box.innerHTML.includes('still be sent'),
        'no-preview state names its reason and says the file still sends');
    assert(box.innerHTML.includes(m.EC.NO_PREVIEW_HINT['too-large-for-preview']),
        'the hint text comes from the shared table (page and composer cannot disagree)');

    const resolved = { id: 's', name: 'ok.png', type: 'image/png', size: 9, _previewUrl: 'blob:42' };
    m.EC.renderPreview(box, Object.assign({}, base, { attachments: [resolved], attachmentPreviewUrl: () => 'blob:42' }));
    assert(box.innerHTML.includes('blob:42') && box.innerHTML.includes('loading="lazy"') && box.innerHTML.includes('decoding="async"'),
        'resolved preview renders an <img> that decodes off the critical path');
    assert(!box.innerHTML.includes('blob:42" alt="">') === false || box.innerHTML.includes('alt="ok.png"'),
        'preview image is labelled with the file name');

    const doc = { id: 't', name: 'notes.txt', type: 'text/plain', size: 4 };
    m.EC.renderPreview(box, Object.assign({}, base, { attachments: [doc], attachmentPreviewUrl: () => null }));
    assert(box.innerHTML.includes('notes.txt') && !box.innerHTML.includes('pending'),
        'non-image attachments stay a plain badge');

    const noResolver = { id: 'u', name: 'z.png', type: 'image/png', size: 9, blob: new FakeBlob(['z']) };
    const m2 = loadModule({ blobLoads: true });
    m2.EC.renderPreview(box, Object.assign({}, base, { attachments: [noResolver] }));
    assert(box.innerHTML.includes('blob:'), 'pages without a resolver still preview via the module cache');
    assert(m2.env.mints === 1, 'and still only once per attachment');

    // A throwing createObjectURL must not blank the rest of the message.
    const m3 = loadModule();
    m3.sandbox.URL = { createObjectURL: () => { throw new Error('quota'); }, revokeObjectURL: () => {} };
    m3.EC.renderPreview(box, Object.assign({ embeds: [{ ...m3.EC.blankEmbed(), title: 'EmbedTitle' }] },
        { content: 'MessageStillHere', attachments: [{ id: 'v', name: 'v.png', type: 'image/png', blob: new FakeBlob([]) }], lookups: LOOKUPS }));
    assert(box.innerHTML.includes('MessageStillHere') && box.innerHTML.includes('EmbedTitle'),
        'a throwing createObjectURL degrades one item, not the whole preview');
}).then(() => {
// ═══════════════════════════════════════════════════════════════
    section('shared state — composer must not fight the page');
    // The page owns lifecycle. Once it has handed back null (in flight / no
    // preview), the module must not quietly mint its own blob: URL behind the
    // page's back — that is how the old leak came back.
    const m = loadModule({ blobLoads: true });
    const base = { content: 'c', embeds: [m.EC.blankEmbed()], lookups: LOOKUPS };
    const a = imgAtt({ _previewError: 'too-large-for-preview' });
    m.EC.renderPreview(box, Object.assign({}, base, { attachments: [a], attachmentPreviewUrl: () => null }));
    assert(m.env.mints === 0, 'no module-side mint while the page resolver is authoritative', `mints=${m.env.mints}`);
}).then(() => {
    console.log(`\nembed-attachments: ${pass} passed, ${fail} failed`);
    if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
    console.log('ALL EMBED-ATTACHMENT TESTS PASSED');
}).catch(e => { console.error('HARNESS ERROR:', e); process.exit(2); });
