#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Embed Builder page logic — attachment preview resolution

   This harness runs the REAL functions out of
   dashboard/templates/manage/embedbuilder.html (extracted from the
   Jinja template, not a copy) against real embed-composer.js, so a
   change to either side that breaks the other gets caught here:

     * looksLikeImage — files whose File.type is empty are still images
     * the sync resolver must never return a stale/bogus URL
     * "in flight" must be a distinct state from "no preview"
     * revoke must clear every transient field
     * blob: and data: tiers resolve to the SAME url for grid and preview
     * a too-large file gets a stated reason, not a blank tile, and is
       never re-decoded on every keystroke

   Run:  node scripts/test_embedbuilder_attachment_logic.js
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
const tick = () => new Promise(r => setTimeout(r, 20));
async function until(fn, ms = 4500) {
    const t0 = Date.now();
    for (;;) {
        try { if (fn()) return true; } catch (e) { /* not there yet */ }
        if (Date.now() - t0 > ms) return false;
        await tick();
    }
}

const ROOT = path.join(__dirname, '..');
const COMPOSER = fs.readFileSync(path.join(ROOT, 'dashboard', 'static', 'js', 'embed-composer.js'), 'utf8');
const TEMPLATE = fs.readFileSync(
    path.join(ROOT, 'dashboard', 'templates', 'manage', 'embedbuilder.html'), 'utf8');

// ── Pull the page's attachment helpers out of the template ──────────
// Declared one per block, terminated by the next top-level declaration
// — no copies, so renaming/adding a guard here fails loudly instead of
// silently testing a stale fork of the code.
const DECL_RE = /^(const|let|function) ([A-Za-z_$][\w$]*)/gm;
function extractPageBlock(decl) {
    const start = TEMPLATE.indexOf('\n' + decl);
    if (start < 0) throw new Error(`embedbuilder.html no longer contains "${decl}" — update this harness`);
    let end = TEMPLATE.length;
    DECL_RE.lastIndex = start + 1;
    let m;
    while ((m = DECL_RE.exec(TEMPLATE))) {
        // the first hit IS the declaration itself (start is the \n before it)
        if (m.index > start + 1) { end = m.index; break; }
    }
    if (end === TEMPLATE.length && !TEMPLATE.slice(start).length) {
        throw new Error(`could not find the end of "${decl}"`);
    }
    return TEMPLATE.slice(start + 1, end).trimEnd() + '\n';
}

const DECLS = [
    'const IMAGE_EXT_RE',
    'const IMAGE_MIME_BY_EXT',
    'function looksLikeImage',
    'function imageMimeFor',
    'function attachmentPreviewHint',
    'let _previewRefreshQueued',
    'function _queuePreviewRefresh',
    'function refreshAttachmentPreview',
    'function getAttachmentPreviewUrl',
    'function revokeAttachmentPreview',
    'function revokeAllAttachmentPreviews',
];
const PAGE = Array.from(new Set(DECLS.map(extractPageBlock))).join('\n');
// Fail loudly if the extraction silently produced an empty block — a harness
// that tests nothing is worse than no harness.
for (const d of DECLS) {
    if (!PAGE.includes(d.replace(/^(const|let|function) /, ''))) {
        throw new Error(`harness could not extract "${d}" from the template`);
    }
}

function build({ blobLoads }) {
    let mints = 0, reads = 0;
    const sandbox = {
        console, Promise, setTimeout, clearTimeout,
        atob: (s) => Buffer.from(s, 'base64').toString('binary'),
        Blob: class { constructor(parts) { this.parts = parts; } },
        document: {},
        // blobLoads=false models a CSP img-src that omits blob:: the src is
        // set, nothing ever paints, and only a timeout reveals the truth.
        Image: class { set src(v) { if (blobLoads) setTimeout(() => this.onload && this.onload(), 0); } },
        FileReader: class {
            readAsDataURL() {
                reads++;
                this.result = 'data:image/png;base64,Z';
                setTimeout(() => this.onload && this.onload(), 0);
            }
        },
        URL: {
            createObjectURL: () => `blob:${++mints}`,
            revokeObjectURL: () => {},
        },
    };
    sandbox.window = {};
    vm.createContext(sandbox);
    vm.runInContext(COMPOSER, sandbox);
    vm.runInContext(`
        const EC = window.EmbedComposer;
        const _ebEsc = EC.esc, _ebAttr = EC.attr;
        var attachments = [];
        var renderAttachmentsCalls = 0, renderPreviewCalls = 0;
        function renderAttachments(){ renderAttachmentsCalls++; }
        function renderPreview(){ renderPreviewCalls++; }
        function showToast(){}
    `, sandbox);
    vm.runInContext(
        '(function(){' + PAGE +
        '\nObject.assign(this,{looksLikeImage,imageMimeFor,getAttachmentPreviewUrl,' +
        'revokeAttachmentPreview,revokeAllAttachmentPreviews,attachmentPreviewHint,' +
        'refreshAttachmentPreview,IMAGE_EXT_RE,IMAGE_MIME_BY_EXT,EC});' +
        '}).call(globalThis);', sandbox);
    return { sandbox, stats: () => ({ mints, reads }) };
}

(async function main() {
// ═══════════════════════════════════════════════════════════════
section('type sniffing — an image with no File.type is still an image');
{
    const { sandbox: sb } = build({ blobLoads: true });
    assert(sb.looksLikeImage({ name: 'a.png', type: 'image/png' }) === true, 'image/png → image');
    assert(sb.looksLikeImage({ name: 'a.txt', type: 'text/plain' }) === false, 'text/plain → not an image');
    assert(sb.looksLikeImage({ name: 'IMG_0291.HEIC', type: '' }) === true, 'empty type + .heic → image (was: invisible forever)');
    assert(sb.looksLikeImage({ name: 'clip.mp4', type: '' }) === false, 'empty type + .mp4 → not an image');
    assert(sb.looksLikeImage({ name: 'x.png', type: 'application/octet-stream' }) === false,
        'a real non-image MIME wins over the extension');
    assert(sb.imageMimeFor('a.JPEG') === 'image/jpeg', 'extension → MIME map is case-insensitive');
    assert(sb.IMAGE_EXT_RE.test('frame.APNG'), 'apng recognised');
}

// ═══════════════════════════════════════════════════════════════
section('blob: tier (normal case)');
{
    const { sandbox: sb, stats } = build({ blobLoads: true });
    const a = { id: 'a1', name: 'p.png', type: 'image/png', size: 100, blob: new sb.Blob(['x']), source: 'local' };
    sb.attachments = [a];
    assert(sb.getAttachmentPreviewUrl(a) === null, 'first call: no cached URL yet, renderers get null');
    assert(a._previewPending === true, 'first call marks the slot pending, not missing');
    assert(await until(() => a._previewUrl), 'blob URL is resolved asynchronously');
    assert(sb.getAttachmentPreviewUrl(a) === a._previewUrl, 'resolver then serves the cached URL');
    assert(await until(() => sb.renderAttachmentsCalls >= 1 && sb.renderPreviewCalls >= 1),
        'resolution re-renders both the grid and the preview');
    const snapshot = stats();
    for (let i = 0; i < 25; i++) sb.getAttachmentPreviewUrl(a);
    assert(stats().mints === snapshot.mints && stats().reads === snapshot.reads,
        '25 further renders mint and read nothing (the per-keystroke leak stays fixed)',
        JSON.stringify(stats()));
    sb.revokeAttachmentPreview(a);
    assert(a._previewUrl === null && a._dataUrl === null && a._previewError === null && a._previewPending === false,
        'revoke clears every transient field');
}

// ═══════════════════════════════════════════════════════════════
section('data: tier (CSP blocks blob:) — preview still appears');
{
    const { sandbox: sb } = build({ blobLoads: false });
    const a = { id: 'b1', name: 'shot.png', type: 'image/png', size: 100, blob: new sb.Blob(['y']), source: 'local' };
    sb.attachments = [a];
    assert(sb.getAttachmentPreviewUrl(a) === null, 'no URL on the first pass');
    assert(await until(() => a._dataUrl), 'falls back to a data: URI and the preview appears anyway');
    assert(a._previewError === null && a._previewPending === false, 'transient flags are cleared on success');
    assert(sb.getAttachmentPreviewUrl(a) === a._dataUrl, 'the data: URI is then served to both renderers');
}

// ═══════════════════════════════════════════════════════════════
section('oversized file — an honest reason, and no re-decode loop');
{
    const { sandbox: sb, stats } = build({ blobLoads: false });
    const big = { id: 'c1', name: 'big.png', type: 'image/png', size: sb.EC.DATA_URL_MAX_BYTES + 1,
        blob: new sb.Blob(['z']), source: 'local' };
    sb.attachments = [big];
    assert(sb.getAttachmentPreviewUrl(big) === null, 'no preview URL');
    assert(await until(() => big._previewError), 'a reason is recorded');
    assert(big._previewError === 'too-large-for-preview', 'reason is too-large-for-preview', String(big._previewError));
    assert(/still be sent/i.test(sb.attachmentPreviewHint(big)),
        'the hint tells the admin the file WILL still be sent');
    const reads = stats().reads;
    for (let i = 0; i < 10; i++) sb.getAttachmentPreviewUrl(big);
    assert(stats().reads === reads, 'a terminal no-preview state is not retried on every keystroke');
}
// ═══════════════════════════════════════════════════════════════
section('CSP img-src must allow blob: (the origin of this bug)');
{
    const APP = fs.readFileSync(path.join(ROOT, 'dashboard', 'app.py'), 'utf8');
    const m = APP.match(/"img-src\s+([^"]+)"/);
    if (!m) {
        assert(false, 'img-src directive found in dashboard/app.py', 'the policy was reformatted — update this guard');
    } else {
        // each header fragment ends with '; ' — strip it before matching
        const tokens = m[1].replace(/[;,]/g, '').trim().split(/\s+/);
        assert(tokens.includes('blob:'), 'blob: is allowed, so createObjectURL previews paint',
            'img-src ' + m[1]);
        assert(tokens.includes('data:'), 'data: is allowed, so the fallback tier works too',
            'img-src ' + m[1]);
        assert(tokens.includes('https:'), 'https: stays allowed (pasted image URLs / Discord CDN)');
    }
}

console.log(`\nembedbuilder-attachments: ${pass} passed, ${fail} failed`);
if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
console.log('ALL EMBED-BUILDER ATTACHMENT TESTS PASSED');
})().catch(e => { console.error('HARNESS ERROR:', e); process.exit(2); });
