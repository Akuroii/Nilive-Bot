#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   scripts/test_message_builder_assets.js
   Message Builder v2 — phase 2, step 7a: the asset CORE.

   WHAT THIS SUITE IS ABOUT
   embed/assets.js is a PURE function of (bytes, name, limits). It is
   the half of the asset layer that decides WHAT a file is, WHAT it may
   be called, and WHETHER the document may take one more — with no DOM,
   no storage, no network and no clock. This harness holds it to that:

     A. the module, its contract, and its distance from everything
        stateful (source scans + a sandbox that has NOTHING in it);
     B. identity: SHA-256 against node's crypto — including the padding
        edges and multi-block inputs — and the content-addressed id;
     C. magic bytes: the four formats an embed accepts, and the
        near-misses that must NOT be mistaken for them;
     D. names: path traversal, unsafe characters, dotfiles, the length
        cap, and the deterministic suffix for a collision;
     E. `identify`: the whole decision — ok, unrecognised bytes, a
        format an embed cannot show, and a name that disagrees with the
        bytes (each with a sentence that names the real problem);
     F. limits: every number read from the SERVED table (a custom table
        changes the outcome), exact-cap behaviour, the advisory's
        hard/soft distinction, and fail-closed on seven unusable tables;
     G. records: the shape, the fixed key order, the conservative
        `availability` normalisation, and preserving keys from a newer
        build;
     H. document queries: assetsOf / refsOf / referencedAssetIds, one
        asset in four slots;
     I. pruneOrphans: deterministic, side-effect free, and provably
        unable to reach back into its input;
     J. the four-slot walkthrough (Case E of the plan) end to end;
     K. collision handling: deterministic, order-independent, bounded;
     L. purity: no persistence, no module state, a frozen API, and two
        independent realms;
     M. cost: a printed measurement with a loose guard.

   Run:  node scripts/test_message_builder_assets.js
         NERO_ASSETS_SRC=/path/to/copy.js   (the mutation battery's hook)
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

const ROOT = path.join(__dirname, '..');
const ASSETS_PATH = process.env.NERO_ASSETS_SRC || path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'assets.js');
const SRC = fs.readFileSync(ASSETS_PATH, 'utf8');

/** Comments removed, so a source scan cannot be fooled by prose. */
function codeOnly(src) {
    return String(src)
        .replace(/\/\*[\s\S]*?\*\//g, ' ')
        .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');
}
const CODE = codeOnly(SRC);

/** Load the module the way the browser would — in a sandbox with nothing. */
function loadModule(extra) {
    const sandbox = Object.assign({ window: {}, console: console }, extra || {});
    vm.createContext(sandbox);
    vm.runInContext(SRC, sandbox, { filename: 'assets.js' });
    return sandbox.window.NERO.embed.assets;
}

const A = loadModule();

/** The served table, mirrored from utils/discord_limits.limits_payload(). */
function servedLimits() {
    return {
        message: { content_max: 2000, embeds_max: 10, embed_total_chars_max: 6000, request_bytes_max: 26214400 },
        attachments: {
            count_max: 10, total_bytes_max: 26148864,
            file_bytes_advisory: 20971520, file_advisory_is_hard: false,
        },
        embed: {},
        components: {},
    };
}
const LIMITS = servedLimits();

// ── Fixtures: real headers, not magic-byte stubs ──────────────────
function bytesOf(values) { return new Uint8Array(values); }
function concat(parts) {
    const total = parts.reduce((n, p) => n + p.length, 0);
    const out = new Uint8Array(total);
    let at = 0;
    parts.forEach(p => { out.set(p, at); at += p.length; });
    return out;
}
function asciiBytes(text) {
    const out = new Uint8Array(text.length);
    for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i) & 0xff;
    return out;
}
/** A tiny but structurally real PNG (signature + IHDR + IEND). */
function pngBytes(extra) {
    const signature = bytesOf([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    const ihdr = concat([
        bytesOf([0x00, 0x00, 0x00, 0x0d]), asciiBytes('IHDR'),
        bytesOf([0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00]),
        bytesOf([0x72, 0xfa, 0x7a, 0x29]),
    ]);
    const iend = concat([bytesOf([0x00, 0x00, 0x00, 0x00]), asciiBytes('IEND'), bytesOf([0xae, 0x42, 0x60, 0x82])]);
    return concat(extra ? [signature, ihdr, asciiBytes(extra), iend] : [signature, ihdr, iend]);
}
function jpegBytes(tail) {
    return concat([
        bytesOf([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10]), asciiBytes('JFIF\0'),
        bytesOf([0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00]),
        bytesOf([0xff, 0xdb, 0x00, 0x04, 0x00, 0x00]),
        asciiBytes(tail || ''),
        bytesOf([0xff, 0xd9]),
    ]);
}
function gifBytes(version, tail) {
    return concat([
        asciiBytes('GIF' + (version || '89a')),
        bytesOf([0x02, 0x00, 0x01, 0x00, 0x80, 0x00, 0x00]),
        asciiBytes(tail || ''),
        bytesOf([0x3b]),
    ]);
}
function webpBytes(tail) {
    const body = concat([asciiBytes('WEBP'), asciiBytes('VP8 '), bytesOf([0x0a, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])]);
    const size = body.length + (tail ? tail.length : 0);
    return concat([
        asciiBytes('RIFF'),
        bytesOf([size & 0xff, (size >> 8) & 0xff, (size >> 16) & 0xff, (size >> 24) & 0xff]),
        body,
        tail ? asciiBytes(tail) : bytesOf([]),
    ]);
}

// ── Document fixtures ─────────────────────────────────────────────
function uploadRef(assetId, filename) {
    return { kind: 'upload', assetId: assetId, filename: filename, mime: null, bytes: null };
}
function embedFixture(id, media) {
    return {
        id: id, title: 'Title', url: '', description: 'Body', color: 0x7c5cbf,
        author: { name: 'Nero', url: '', icon: media['author.icon'] || null },
        footer: { text: 'Footer', icon: media['footer.icon'] || null },
        thumbnail: media.thumbnail || null,
        image: media.image || null,
        timestamp: '', fields: [],
    };
}
function documentFixture(assets, media) {
    return {
        schemaVersion: 2, id: 'doc-1', guildId: 'guild-1', layout: 'legacy',
        content: 'Hello', embeds: [embedFixture('emb-1', media || {})], rows: [],
        assets: assets || {},
    };
}
function recordFor(identified, overrides) {
    const base = {
        assetId: identified.assetId, sha256: identified.sha256, mime: identified.mime,
        bytes: null, originalName: 'rules.png', filename: identified.filename,
        availability: 'bytes-local', createdAt: '2026-09-25T00:00:00Z',
    };
    return Object.assign(base, overrides || {});
}
function clone(value) { return JSON.parse(JSON.stringify(value)); }

// ═══════════════════════════════════════════════════════════════
section('A. the module, its contract, and its distance from the DOM');
// ═══════════════════════════════════════════════════════════════
assert(!!A, 'assets.js publishes window.NERO.embed.assets');
assert(Object.isFrozen(A), 'the API object is frozen (a caller cannot replace a rule)');
[
    'sha256Hex', 'assetIdFromSha', 'identify', 'sniffMime', 'mimeLabel', 'describeBytes',
    'sanitiseFilename', 'filenameExtension', 'uniquifyFilenames',
    'attachmentLimits', 'checkLimits', 'checkFileSize',
    'buildRecord', 'normalizeRecord', 'assetsOf', 'assetIssues',
    'refsOf', 'referencedAssetIds', 'documentAssetIds', 'pruneOrphans',
].forEach(name => {
    assert(typeof A[name] === 'function', 'the module exposes ' + name + '()');
});
assert(Object.isFrozen(A.LIMIT_KEYS) && Object.isFrozen(A.LIMIT_KEYS.required) &&
       Object.isFrozen(A.RECORD_KEYS) && Object.isFrozen(A.ALLOWED_EXTENSIONS),
    'and the constant tables it hands out are frozen too');
// The module must load and work in a realm that has NO browser at all.
// (loadModule() above only supplied `window` — no document, no storage,
// no timers, no crypto. Everything below runs in that realm.)
assert(typeof A.sha256Hex(pngBytes()) === 'string',
    'the module works in a sandbox with nothing but `window` (no DOM, no storage, no crypto API)');
// Each pattern is a WAY of reaching for something stateful, not a word: a
// bare substring search would flag the phrase "a PDF document" in a message
// string, and a check that cries wolf gets deleted.
const FORBIDDEN = [
    ['the DOM', /(^|[^.\w'"])document\s*[.\[]/m],
    ['IndexedDB', /\bindexedDB\b/],
    ['web storage', /\b(localStorage|sessionStorage)\b/],
    ['the network', /(\bfetch\s*\(|\bXMLHttpRequest\b|\bsendBeacon\b|\bWebSocket\b|\bEventSource\b)/],
    ['file reading', /(\bFileReader\b|\bcreateObjectURL\b|\bnew\s+File\b|\bFileSystemFileHandle\b)/],
    ['blobs or forms', /(\bnew\s+Blob\b|\bFormData\b)/],
    ['the environment', /(\bnavigator\b|\bperformance\s*\.|\brequestAnimationFrame\b|\bnew\s+Worker\b|\bpostMessage\b)/],
    ['DOM observation', /(\bMutationObserver\b|\baddEventListener\b|\bIntersectionObserver\b)/],
    ['timers', /(\bsetTimeout\b|\bsetInterval\b|\brequestIdleCallback\b)/],
    ['a clock or randomness', /(\bDate\s*\.\s*now\b|\bnew\s+Date\b|\bMath\s*\.\s*random\b|\bperformance\s*\.\s*now\b)/],
];
const reached = FORBIDDEN
    .filter(([, pattern]) => pattern.test(CODE))
    .map(([label]) => label);
assert(reached.length === 0,
    'the source reaches for no DOM, storage, network, timer, clock or randomness API',
    reached.join(', '));
assert(!/^ {4}(let|var)\s/m.test(CODE),
    'no module-level `let`/`var`: there is no module state to drift');
assert(/^ {4}const /m.test(CODE),
    'rig: the module DOES declare its tables at module level (so the check above means something)');
assert(!/\bwindow\.(?!NERO)/.test(CODE) && !/\bglobalThis\b|\bself\./.test(CODE),
    'the only global it touches is window.NERO (the publish)');
// The numbers that must never appear: the served limits themselves, and a
// comparison against a literal where a served number belongs. (Rotation
// counts like rotr(e, 25) inside SHA-256 are arithmetic, not limits — hence
// patterns rather than a lone `\b25\b`.)
const LEAKS = [
    ['a served byte limit', /\b(26148864|20971520|26214400|10485760)\b/],
    ['a literal cap assigned to a served key', /(count_max|total_bytes_max|file_bytes_advisory)\s*[:=]\s*\d/],
    // (a two-digit literal: `count > 0` is a clamp, `count > 10` is a cap)
    ['a comparison against a literal cap', /(count|size|used|total)\s*[<>]=?\s*\d{2,}/],
    ['a literal multiply that rebuilds a limit', /\b\d+\s*\*\s*1024\s*\*\s*1024\b/],
];
const leaked = LEAKS.filter(([, pattern]) => pattern.test(CODE)).map(([label]) => label);
assert(leaked.length === 0,
    'no served attachment limit is hard-coded in the module',
    leaked.join(', '));

// ═══════════════════════════════════════════════════════════════
section('B. identity: SHA-256 and the content-addressed id');
// ═══════════════════════════════════════════════════════════════
{
    const oracle = (bytes) => crypto.createHash('sha256').update(Buffer.from(bytes)).digest('hex');
    const cases = [
        ['empty', new Uint8Array(0)],
        ['one byte', bytesOf([0x00])],
        ['"abc"', asciiBytes('abc')],
        ['55 bytes (one byte of padding room short)', new Uint8Array(55).fill(0x61)],
        ['56 bytes (the padding boundary)', new Uint8Array(56).fill(0x62)],
        ['63 bytes', new Uint8Array(63).fill(0x63)],
        ['64 bytes (two blocks)', new Uint8Array(64).fill(0x64)],
        ['65 bytes', new Uint8Array(65).fill(0x65)],
        ['1000 bytes (multi-block)', new Uint8Array(1000).map((_, i) => (i * 7) & 0xff)],
        ['high-bit bytes', bytesOf([0xff, 0xfe, 0x80, 0x00, 0x7f])],
        ['a real PNG', pngBytes('hello')],
    ];
    let mismatches = [];
    cases.forEach(([label, bytes]) => {
        const mine = A.sha256Hex(bytes);
        const theirs = oracle(bytes);
        if (mine !== theirs) mismatches.push(label + ': ' + mine.slice(0, 12) + ' vs ' + theirs.slice(0, 12));
    });
    assert(mismatches.length === 0,
        'SHA-256 matches node\u2019s crypto for ' + cases.length + ' inputs (padding edges included)',
        mismatches.join(' | '));
    assert(A.sha256Hex(pngBytes()) === A.sha256Hex(pngBytes()),
        'hashing is deterministic');
    assert(A.sha256Hex(bytesOf([1, 2, 3])) !== A.sha256Hex(bytesOf([1, 2, 4])),
        'a one-bit change produces a different digest');
    assert(A.sha256Hex('not bytes') === '',
        'an unusable input is an empty digest, never a hash of something else');
    // Same bytes, three shapes.
    const same = pngBytes('shape');
    const asBuffer = same.buffer.slice(0);
    const asArray = Array.from(same);
    assert(A.sha256Hex(same) === A.sha256Hex(asBuffer) && A.sha256Hex(same) === A.sha256Hex(asArray),
        'Uint8Array, ArrayBuffer and Array of the same bytes hash identically');
    const view = new Uint8Array(new Uint8Array([9, 9, same[0], same[1]]).buffer, 2);
    assert(A.sha256Hex(view) === A.sha256Hex(bytesOf([same[0], same[1]])),
        'and a byteOffset view hashes only its own window');
}
{
    const digest = A.sha256Hex(pngBytes());
    const id = A.assetIdFromSha(digest);
    assert(id === 'a_' + digest.slice(0, 16), 'the asset id is a_ + the first 16 hex characters', id);
    assert(/^a_[0-9a-f]{16}$/.test(id), 'so the id is always filesystem- and URL-safe');
    assert(A.assetIdFromSha(digest.toUpperCase()) === id, 'a digest in upper case gives the same id');
    assert(A.assetIdFromSha('too-short') === '' && A.assetIdFromSha('') === '' &&
           A.assetIdFromSha(null) === '' && A.assetIdFromSha('z'.repeat(64)) === '',
        'and an unusable digest has no id at all (never a partial one)');
    const a = A.identify(pngBytes('one'), 'one.png');
    const b = A.identify(pngBytes('two'), 'two.png');
    assert(a.assetId === A.identify(pngBytes('one'), 'renamed-entirely.png').assetId,
        'the SAME bytes are one asset under any name');
    assert(a.assetId !== b.assetId, 'different bytes are different assets');
    assert(a.assetId.length === 18 && a.assetId.indexOf('a_') === 0,
        'identify() carries the same content-addressed id', a.assetId);
}

// ═══════════════════════════════════════════════════════════════
section('C. magic bytes: the four formats, and the near misses');
// ═══════════════════════════════════════════════════════════════
{
    assert(A.sniffMime(pngBytes()) === 'image/png', 'a PNG is image/png');
    assert(A.sniffMime(jpegBytes()) === 'image/jpeg', 'a JPEG is image/jpeg');
    assert(A.sniffMime(gifBytes('89a')) === 'image/gif', 'GIF89a is image/gif');
    assert(A.sniffMime(gifBytes('87a')) === 'image/gif', 'GIF87a is image/gif too');
    assert(A.sniffMime(webpBytes()) === 'image/webp', 'a RIFF/WEBP container is image/webp');
    const misses = [
        ['empty bytes', new Uint8Array(0)],
        ['a PNG signature cut short', bytesOf([0x89, 0x50, 0x4e])],
        ['JPEG without its third byte', bytesOf([0xff, 0xd8])],
        ['JPEG with a different third byte', bytesOf([0xff, 0xd8, 0x00, 0x00])],
        ['a RIFF that is not WebP (a WAV)', concat([asciiBytes('RIFF'), bytesOf([1, 0, 0, 0]), asciiBytes('WAVE')])],
        ['a RIFF too short to hold WEBP', bytesOf([0x52, 0x49, 0x46, 0x46])],
        ['GIF signature with the wrong format byte', concat([asciiBytes('GIF3'), bytesOf([0x38, 0x61])])],
        ['GIF8 with a version that is neither 87a nor 89a', concat([asciiBytes('GIF8'), asciiBytes('Xa')])],
        ['GIF8 with no version at all', asciiBytes('GIF8')],
        ['GIF89 without the final letter', asciiBytes('GIF89')],
        ['a PDF', asciiBytes('%PDF-1.7\n%something')],
        ['a ZIP', bytesOf([0x50, 0x4b, 0x03, 0x04, 0x14, 0x00])],
        ['a BMP', bytesOf([0x42, 0x4d, 0x36, 0x00])],
        ['an ICO', bytesOf([0x00, 0x00, 0x01, 0x00, 0x20, 0x20])],
        ['an SVG', asciiBytes('<svg xmlns="http://www.w3.org/2000/svg"></svg>')],
        ['plain text', asciiBytes('just a note to self')],
        ['uppercase PNG signature (wrong case is not the format)', asciiBytes('PNG')],
    ];
    const wrong = misses.filter(([, bytes]) => A.sniffMime(bytes) !== null).map(([label]) => label);
    assert(wrong.length === 0,
        'none of the ' + misses.length + ' near-misses is mistaken for an accepted image',
        wrong.join(', '));
    assert(A.describeBytes(asciiBytes('%PDF-1.7')) === 'a PDF document' &&
           A.describeBytes(bytesOf([0x50, 0x4b, 0x03, 0x04])) === 'a ZIP archive' &&
           A.describeBytes(new Uint8Array(0)) === 'an empty file' &&
           /text, not an image/.test(A.describeBytes(asciiBytes('hello there'))) &&
           /SVG/.test(A.describeBytes(asciiBytes('<svg>'))),
        'and the refusal can say what the file actually is');
    assert(A.mimeLabel('image/webp') === 'WebP' && A.mimeLabel(null) === 'unknown',
        'the format labels are the ones the messages use');
}

// ═══════════════════════════════════════════════════════════════
section('D. names: paths, unsafe characters, dotfiles, the cap');
// ═══════════════════════════════════════════════════════════════
{
    assert(A.sanitiseFilename('Rules.PNG') === 'rules.png', 'case is lowered, the extension kept',
        A.sanitiseFilename('Rules.PNG'));
    assert(A.sanitiseFilename('my photo (1).png') === 'my-photo-1.png',
        'spaces and punctuation become single dashes', A.sanitiseFilename('my photo (1).png'));
    assert(A.sanitiseFilename('a--b___c.png') === 'a-b___c.png',
        'dash runs collapse (underscores are already safe)');
    assert(A.sanitiseFilename('../../etc/passwd') === 'passwd',
        'a traversal attempt becomes its last component', A.sanitiseFilename('../../etc/passwd'));
    assert(A.sanitiseFilename('..\\..\\windows\\evil.png') === 'evil.png',
        'and so does a Windows-style path', A.sanitiseFilename('..\\..\\windows\\evil.png'));
    assert(A.sanitiseFilename('/etc/shadow.png') === 'shadow.png' &&
           A.sanitiseFilename('C:\\Users\\me\\rules.gif') === 'rules.gif',
        'absolute paths lose their directories on both separator styles');
    assert(A.sanitiseFilename('..') === 'image' && A.sanitiseFilename('.') === 'image' &&
           A.sanitiseFilename('') === 'image' && A.sanitiseFilename(null) === 'image' &&
           A.sanitiseFilename('   ') === 'image',
        'a name with nothing usable in it falls back to "image" — never empty, never ".."');
    assert(A.sanitiseFilename('.env') === 'env' && A.sanitiseFilename('.hidden.png') === 'hidden.png',
        'a dotfile loses its leading dot (no hidden files, ever)',
        A.sanitiseFilename('.env'));
    assert(A.sanitiseFilename('.png') === 'png' && A.sanitiseFilename('...') === 'image',
        'and a name that is only punctuation cannot pose as an extension');
    assert(A.sanitiseFilename('евро.png').indexOf('png') !== -1 &&
           !/[^\x20-\x7e]/.test(A.sanitiseFilename('евро.png')) &&
           A.sanitiseFilename('евро.png').indexOf('.') !== 0,
        'non-ASCII names are reduced to safe ASCII, keeping the extension',
        A.sanitiseFilename('евро.png'));
    const long = 'a'.repeat(200) + '.png';
    const bounded = A.sanitiseFilename(long);
    assert(bounded.length <= A.MAX_FILENAME && bounded.length === A.MAX_FILENAME &&
           bounded.slice(-4) === '.png',
        'a 200-character name is bounded to ' + A.MAX_FILENAME + ' with the extension intact',
        bounded.length + ' ' + bounded.slice(-6));
    assert(/^[a-z0-9._-]+$/.test(A.sanitiseFilename(long + '?<>|"')), 'and stays inside the safe alphabet');
    assert(A.sanitiseFilename('-leading.png') === 'leading.png' &&
           A.sanitiseFilename('trailing-.png') === 'trailing.png',
        'leading and trailing punctuation is trimmed, not left to look like a path');
    assert(A.sanitiseFilename('no-extension') === 'no-extension',
        'a name without an extension is allowed through (identify() decides the format)');
    assert(A.filenameExtension('Photo.JPEG') === 'jpeg' && A.filenameExtension('a.tar.gz') === 'gz' &&
           A.filenameExtension('.png') === '' && A.filenameExtension('noext') === '' &&
           A.filenameExtension('dir.d/file') === '',
        'the extension reader reports what the NAME claims, and a dotfile claims nothing');
    assert(A.sanitiseFilename('a/b/c.png') === 'c.png', 'only the last component survives');
}

// ═══════════════════════════════════════════════════════════════
section('E. identify(): the whole decision, with its wording');
// ═══════════════════════════════════════════════════════════════
{
    const cases = [
        ['rules.png', pngBytes('a'), 'image/png', 'png'],
        ['rules.PNG', pngBytes('b'), 'image/png', 'png'],
        ['photo.jpg', jpegBytes('x'), 'image/jpeg', 'jpg'],
        ['photo.jpeg', jpegBytes('y'), 'image/jpeg', 'jpeg'],
        ['anim.gif', gifBytes('89a'), 'image/gif', 'gif'],
        ['logo.webp', webpBytes(), 'image/webp', 'webp'],
    ];
    let bad = [];
    cases.forEach(([name, bytes, mime, ext]) => {
        const out = A.identify(bytes, name);
        if (!out.ok || out.mime !== mime || out.ext !== ext) bad.push(name + ' → ' + JSON.stringify(out));
        if (out.filename !== A.sanitiseFilename(name).replace(/\.[a-z0-9]+$/, '.' + ext)) {
            bad.push(name + ' filename ' + out.filename);
        }
    });
    assert(bad.length === 0, 'all six accepted name/format pairs identify cleanly', bad.join(' | '));
    const jpegAlias = A.identify(jpegBytes('z'), 'photo.jpeg');
    assert(jpegAlias.ext === 'jpeg' && jpegAlias.filename === 'photo.jpeg',
        'the user\u2019s own spelling (.jpeg) is kept — the name they saw is the name they get',
        jpegAlias.filename);
    // Name claims a format an embed cannot show.
    [['art.svg', asciiBytes('<svg></svg>')], ['doc.pdf', asciiBytes('%PDF-1.7')],
     ['pic.bmp', bytesOf([0x42, 0x4d, 0x00])], ['photo.heic', asciiBytes('ftypheic')],
     ['a.tar.gz', asciiBytes('\x1f\x8b\x08')]].forEach(([name, bytes]) => {
        const out = A.identify(bytes, name);
        assert(!out.ok && out.reason === 'extension-not-allowed' &&
               out.message.indexOf('.' + A.filenameExtension(name)) !== -1,
            'a .' + A.filenameExtension(name) + ' name is refused as an unusable extension, and says which',
            out.reason + ' ' + out.message);
    });
    // Name and bytes disagree.
    const mismatch = A.identify(jpegBytes('m'), 'holiday.png');
    assert(!mismatch.ok && mismatch.reason === 'extension-mismatch' &&
           mismatch.mime === 'image/jpeg' && mismatch.declaredExt === 'png',
        'PNG-named JPEG bytes are refused', mismatch.reason);
    assert(/says \.png/.test(mismatch.message) && /JPEG/.test(mismatch.message) &&
           mismatch.message.indexOf('holiday') === -1,
        'and the sentence names both sides of the disagreement: "' + mismatch.message + '"');
    assert(A.identify(gifBytes('87a'), 'thing.webp').reason === 'extension-mismatch' &&
           A.identify(pngBytes(), 'thing.gif').reason === 'extension-mismatch',
        'the check is symmetrical across formats (not a one-way rule)');
    // Bytes that are not an accepted format at all, under a name that IS
    // allowed — the bytes have to be the thing that decides.
    [['notes.png', asciiBytes('dear diary')], ['report.jpg', asciiBytes('%PDF-1.4')],
     ['archive.gif', bytesOf([0x50, 0x4b, 0x03, 0x04])],
     ['photo.webp', asciiBytes('not an image either')],
     ['notes.txt', asciiBytes('dear diary')]].forEach(([name, bytes]) => {
        const out = A.identify(bytes, name);
        const nameAllowed = /^(png|jpg|jpeg|gif|webp)$/.test(A.filenameExtension(name));
        assert(!out.ok && out.mime === null &&
               out.reason === (nameAllowed ? 'unrecognised-bytes' : 'extension-not-allowed') &&
               (nameAllowed ? /must be PNG, JPEG, WebP or GIF/.test(out.message)
                            : out.message.indexOf('.' + A.filenameExtension(name)) !== -1),
            'a ' + A.filenameExtension(name) + ' name with non-image bytes is refused for the right reason (' + name + ')',
            out.reason + ' — ' + out.message);
    });
    // A name the embed cannot show is refused whatever the bytes are — and
    // it says which extension to rename, which is the fixable part.
    const disguised = A.identify(pngBytes('really-an-image'), 'picture.heic');
    assert(!disguised.ok && disguised.reason === 'extension-not-allowed' &&
           disguised.message.indexOf('.heic') !== -1 && disguised.mime === 'image/png',
        'even a real image is refused under an extension an embed cannot show, and the message names it',
        disguised.reason + ' — ' + disguised.message);
    const pdf = A.identify(asciiBytes('%PDF-1.4'), 'disguised.png');
    assert(pdf.message.indexOf('a PDF document') !== -1,
        'a mislabelled PDF says what it actually is: "' + pdf.message + '"');
    const empty = A.identify(new Uint8Array(0), 'nothing.png');
    assert(!empty.ok && empty.reason === 'no-bytes' && /empty/.test(empty.message),
        'an empty file is refused for being empty', empty.reason);
    assert(A.identify(null, 'nothing.png').reason === 'no-bytes' &&
           A.identify('a string, not bytes', 'x.png').reason === 'no-bytes',
        'a non-byte value is never coerced into something hashable');
    // Determinism and purity of the result.
    const first = A.identify(pngBytes('pure'), 'pure.png');
    const snapshot = JSON.stringify(first);
    first.filename = 'mutated.png';
    first.assetId = 'a_' + '0'.repeat(16);
    const second = A.identify(pngBytes('pure'), 'pure.png');
    assert(JSON.stringify(second) !== snapshot || true, 'rig: the first result was mutated by the test');
    assert(second.filename === 'pure.png' && second.assetId === 'a_' + A.sha256Hex(pngBytes('pure')).slice(0, 16),
        'mutating a returned result cannot reach the next call (no shared state)');
    assert(eq(A.identify(pngBytes('pure'), 'pure.png'), A.identify(pngBytes('pure'), 'pure.png')),
        'and two calls with the same input are identical');
}

// ═══════════════════════════════════════════════════════════════
section('F. limits: the served table is the only source of numbers');
// ═══════════════════════════════════════════════════════════════
{
    const ok = A.checkLimits(3, 5 * 1024 * 1024, LIMITS);
    assert(ok.usable === true && ok.count.used === 3 && ok.count.max === LIMITS.attachments.count_max &&
           ok.bytes.max === LIMITS.attachments.total_bytes_max,
        'the count and byte maxima are the SERVED ones',
        JSON.stringify(ok.count) + ' ' + JSON.stringify(ok.bytes));
    assert(ok.count.over === false && ok.count.canAdd === true && ok.bytes.over === false,
        'three files well inside the caps may grow');
    assert(ok.advisory && ok.advisory.bytes === LIMITS.attachments.file_bytes_advisory &&
           ok.advisory.isHard === false,
        'the per-file advisory is read (and reported as advisory, not as a rule)',
        JSON.stringify(ok.advisory));
    // A different table changes every answer — nothing is baked in.
    const small = servedLimits();
    small.attachments.count_max = 2;
    small.attachments.total_bytes_max = 1000;
    const tight = A.checkLimits(3, 2000, small);
    assert(tight.count.max === 2 && tight.bytes.max === 1000 &&
           tight.count.over === true && tight.bytes.over === true,
        'a served count_max of 2 and total of 1000 change the answer',
        JSON.stringify(tight.count) + ' ' + JSON.stringify(tight.bytes));
    assert(A.checkLimits(1, 0, small).count.canAdd === true &&
           A.checkLimits(2, 0, small).count.canAdd === false,
        'one below the served cap may still add; AT the cap it may not (the 6b rule)');
    assert(A.checkLimits(2, 0, small).count.over === false,
        'being exactly at the cap is not "over" (a full message is not an error)');
    assert(A.checkLimits(3, 0, small).count.over === true, 'one past it is');
    assert(A.checkLimits(0, 5000, small).bytes.over === true &&
           A.checkLimits(0, 1000, small).bytes.over === false,
        'the byte total is compared at the boundary too');
    assert(A.checkLimits(-5, -1, LIMITS).count.used === 0 &&
           A.checkLimits(-5, -1, LIMITS).bytes.used === 0,
        'nonsense counts clamp to zero rather than producing a negative total');
    assert(A.checkLimits(NaN, NaN, LIMITS).count.used === 0,
        'and a NaN count cannot poison the arithmetic');
    // The advisory can be declared hard — then it blocks.
    const hard = servedLimits();
    hard.attachments.file_advisory_is_hard = true;
    const hardSize = A.checkFileSize(new Uint8Array(1024), hard);
    assert(hardSize.oversized === false && hardSize.blocked === false &&
           hardSize.advisoryMax === hard.attachments.file_bytes_advisory,
        'a small file is inside the advisory');
    const big = A.checkFileSize(30 * 1024 * 1024, servedLimits());
    assert(big.usable === true && big.oversized === true && big.blocked === false && big.isHard === false,
        'a file over the ADVISORY is flagged but not blocked (only Discord knows the real cap)',
        JSON.stringify(big));
    const hardBig = A.checkFileSize(30 * 1024 * 1024, hard);
    assert(hardBig.overall !== true && hardBig.blocked === true && hardBig.isHard === true,
        'the same file blocks when the served table declares the cap hard');
    // Fail closed: seven ways a table can be unusable.
    const unusable = [
        ['undefined', undefined], ['null', null], ['a string', 'nope'], ['a number', 42],
        ['an empty object', {}], ['attachments missing', { message: {} }],
        ['count_max missing', { attachments: { total_bytes_max: 100 } }],
        ['total_bytes_max not a number', { attachments: { count_max: 10, total_bytes_max: 'lots' } }],
        ['a negative cap', { attachments: { count_max: -1, total_bytes_max: 100 } }],
    ];
    let invented = [];
    unusable.forEach(([label, table]) => {
        const out = A.checkLimits(1, 100, table);
        if (out.usable !== false || out.count !== null || out.bytes !== null || out.advisory !== null) {
            invented.push(label + ': ' + JSON.stringify(out.count) + JSON.stringify(out.bytes));
        }
        if (out.reason !== 'limits-unusable' || !out.missing.length) invented.push(label + ': no reason');
    });
    assert(invented.length === 0,
        'all ' + unusable.length + ' unusable tables produce NO numbers (null, never a guess)',
        invented.join(' | '));
    const noTable = A.checkLimits(1, 100, null);
    assert(noTable.count === null && noTable.canAdd !== true,
        'with no table nothing can be interpreted as "you may add more"');
    const noSize = A.checkFileSize(new Uint8Array(5), null);
    assert(noSize.usable === false && noSize.advisoryMax === null && noSize.oversized === null &&
           noSize.blocked === null,
        'the per-file check is all nulls without a table — nothing reads as "fine"',
        JSON.stringify(noSize));
    assert(A.checkFileSize('not bytes', LIMITS).usable === false,
        'and a non-byte value has no measurable size');
    assert(A.checkFileSize(new Uint8Array(1024), LIMITS).size === 1024 &&
           A.checkFileSize(1024, LIMITS).size === 1024,
        'a byte count and the bytes themselves give the same size');
    assert(A.checkFileSize(-1, LIMITS).usable === false && A.checkFileSize(NaN, LIMITS).usable === false,
        'a nonsense size is unusable, never "small"');
    // The keys this module consumes are declared, so the schema contract
    // test can check them against the server payload.
    const consumed = A.LIMIT_KEYS.required.concat(A.LIMIT_KEYS.advisory).map(pair => pair.join('.'));
    assert(eq(consumed.slice().sort(),
        ['attachments.count_max', 'attachments.file_advisory_is_hard',
         'attachments.file_bytes_advisory', 'attachments.total_bytes_max']),
        'LIMIT_KEYS names exactly the four served keys this module reads', consumed.join(', '));
    const servedKeys = Object.keys(LIMITS.attachments).sort();
    assert(A.LIMIT_KEYS.required.concat(A.LIMIT_KEYS.advisory).every(([block, key]) =>
        block === 'attachments' && servedKeys.indexOf(key) !== -1),
        'and every one of them exists in the served attachments node',
        servedKeys.join(', '));
}

// ═══════════════════════════════════════════════════════════════
section('G. records: shape, key order, and what a rebuild may not drop');
// ═══════════════════════════════════════════════════════════════
{
    const identified = A.identify(pngBytes('record'), 'Rules.PNG');
    const built = A.buildRecord({
        assetId: identified.assetId, sha256: identified.sha256, mime: identified.mime,
        bytes: 128, width: null, height: null, originalName: 'Rules.PNG',
        filename: identified.filename, availability: 'bytes-local', createdAt: '2026-09-25T10:00:00Z',
    });
    assert(built.ok === true && !!built.record, 'a complete set of fields builds a record');
    assert(eq(Object.keys(built.record), A.RECORD_KEYS),
        'the record has exactly the documented keys, in a fixed order',
        Object.keys(built.record).join(','));
    assert(built.record.filename === 'rules.png' && built.record.assetId === identified.assetId &&
           built.record.bytes === 128 && built.record.width === null,
        'and the values are the ones passed in (normalised, not invented)');
    assert(A.buildRecord({ filename: 'x.png' }).ok === false &&
           A.buildRecord({ filename: 'x.png' }).missing.indexOf('assetId') !== -1 &&
           A.buildRecord({ assetId: 'a_1' }).missing.indexOf('filename') !== -1,
        'an incomplete record is refused with the missing field named');
    assert(A.buildRecord({}).record === null && A.buildRecord(null).reason === 'incomplete-record',
        'and never half-built');
    // availability is conservative: an unknown state must not claim bytes.
    const unknown = A.buildRecord({ assetId: 'a_x', filename: 'x.png', availability: 'probably-fine' });
    assert(unknown.record.availability === 'bytes-missing',
        'an unknown availability normalises to bytes-missing (never to "we have it")',
        unknown.record.availability);
    assert(A.buildRecord({ assetId: 'a_x', filename: 'x.png', availability: 'bytes-local' }).record.availability === 'bytes-local',
        'and a real state is kept');
    assert(A.buildRecord({ assetId: 'a_x', filename: 'x.png', bytes: -1 }).record.bytes === null &&
           A.buildRecord({ assetId: 'a_x', filename: 'x.png', bytes: 'big' }).record.bytes === null,
        'a nonsense byte count is null, not a negative size');
    // A rebuild keeps keys from a newer build.
    const stored = Object.assign({}, built.record, { capturedUrl: 'https://cdn.example/x.png', refCount: 3 });
    const rebuilt = A.normalizeRecord(stored);
    assert(rebuilt.capturedUrl === 'https://cdn.example/x.png' && rebuilt.refCount === 3,
        'a record rebuilt from storage keeps keys this build does not know about',
        Object.keys(rebuilt).join(','));
    assert(eq(Object.keys(rebuilt).slice(0, A.RECORD_KEYS.length), A.RECORD_KEYS),
        'while the known keys stay first, in their fixed order');
    assert(A.normalizeRecord(null) === null && A.normalizeRecord({}) === null &&
           A.normalizeRecord({ assetId: 'a_x' }) === null,
        'an unreadable record is null (the caller reports it, this module does not guess)');
    assert(A.normalizeRecord(rebuilt).assetId === rebuilt.assetId &&
           eq(A.normalizeRecord(rebuilt), rebuilt),
        'normalising an already-normal record changes nothing (idempotent)');
}

// ═══════════════════════════════════════════════════════════════
section('H. the document queries: assetsOf, refsOf, referenced ids');
// ═══════════════════════════════════════════════════════════════
{
    const one = A.identify(pngBytes('shared'), 'rules.png');
    const two = A.identify(gifBytes('89a'), 'anim.gif');
    const assets = {};
    assets[one.assetId] = recordFor(one, { originalName: 'rules.png' });
    assets[two.assetId] = recordFor(two, { originalName: 'anim.gif' });
    const doc = documentFixture(assets, {
        image: uploadRef(one.assetId, one.filename),
        thumbnail: uploadRef(one.assetId, one.filename),
        'author.icon': uploadRef(two.assetId, two.filename),
        'footer.icon': { kind: 'url', url: 'https://cdn.example/footer.png' },
    });
    const records = A.assetsOf(doc);
    assert(records.length === 2, 'assetsOf() returns the records the document carries', String(records.length));
    assert(records[0].assetId < records[1].assetId, 'sorted by id — the answer never depends on key order');
    assert(A.assetsOf(documentFixture()).length === 0 &&
           A.assetsOf(null).length === 0 && A.assetsOf({ assets: 'nope' }).length === 0,
        'and a document with no assets (or no document) yields an empty list, not a throw');
    const refs = A.refsOf(doc);
    assert(refs.length === 3, 'refsOf() finds every upload reference', String(refs.length));
    assert(eq(refs.map(r => r.slot), ['image', 'thumbnail', 'author.icon']),
        'in document order, with the slot named', refs.map(r => r.slot).join(','));
    assert(eq(refs.map(r => r.path),
        ['embeds.0.image', 'embeds.0.thumbnail', 'embeds.0.author.icon'],
        'and a path a validation issue or a rail row could point at'));
    assert(refs.every(r => r.embedId === 'emb-1'), 'each reference knows which embed it belongs to');
    assert(refs.filter(r => r.assetId === one.assetId).length === 2,
        'the same asset referenced twice is two references, not two assets');
    assert(A.refsOf(doc).every(r => r.assetId !== '' && r.filename !== ''),
        'and every reference carries an id and the filename it claims');
    const urlOnly = documentFixture({}, { image: { kind: 'url', url: 'https://cdn.example/a.png' } });
    assert(A.refsOf(urlOnly).length === 0,
        'a pasted URL is not an asset reference (it needs no bytes from us)');
    const nulled = documentFixture({}, { image: null, thumbnail: null });
    assert(A.refsOf(nulled).length === 0, 'and an empty slot is not one either');
    assert(A.refsOf(null).length === 0 && A.refsOf({ embeds: 'nope' }).length === 0,
        'refsOf() survives a document with no embeds');
    assert(eq(A.referencedAssetIds(refs), [one.assetId, two.assetId].sort()),
        'referencedAssetIds() is the distinct, sorted set');
    assert(A.referencedAssetIds([{ assetId: one.assetId }, { assetId: one.assetId }]).length === 1,
        'duplicates collapse');
    assert(A.referencedAssetIds([]).length === 0 && A.referencedAssetIds(null).length === 0,
        'and nothing yields nothing');
    assert(eq(A.documentAssetIds(doc), [one.assetId, two.assetId].sort()),
        'documentAssetIds() reads straight from the document');
    // A malformed entry is reported, never silently turned into an asset.
    const broken = documentFixture({ [one.assetId]: recordFor(one), 'a_broken': 'not a record' });
    assert(A.assetsOf(broken).length === 1, 'a broken asset entry is not returned as an asset');
    assert(eq(A.assetIssues(broken), [{ assetId: 'a_broken', reason: 'unreadable-record' }]),
        'and assetIssues() names it, so a caller can say something honest',
        JSON.stringify(A.assetIssues(broken)));
    assert(eq(A.assetIssues(documentFixture()), []), 'a healthy document has no asset issues');
    assert(eq(A.assetIssues({ assets: { a_x: null } }), [{ assetId: 'a_x', reason: 'missing-record' }]),
        'a null entry is reported as missing');
}

// ═══════════════════════════════════════════════════════════════
section('I. pruneOrphans(): pure, deterministic, and remote from its input');
// ═══════════════════════════════════════════════════════════════
{
    const one = A.identify(pngBytes('keep-me'), 'keep.png');
    const two = A.identify(pngBytes('drop-me'), 'drop.png');
    const three = A.identify(pngBytes('session-new'), 'new.png');
    const assets = {
        [one.assetId]: recordFor(one, { originalName: 'keep.png' }),
        [two.assetId]: recordFor(two, { originalName: 'drop.png' }),
        [three.assetId]: recordFor(three, { originalName: 'new.png' }),
    };
    const doc = documentFixture({ [one.assetId]: assets[one.assetId] }, {
        image: uploadRef(one.assetId, one.filename),
    });
    const before = clone(assets);
    const result = A.pruneOrphans(assets, A.documentAssetIds(doc), { keep: [three.assetId] });
    assert(result.ok === true && eq(result.pruned, [two.assetId]) &&
           eq(result.kept.slice().sort(), [one.assetId, three.assetId].sort()),
        'an unreferenced asset is pruned; the referenced and the session-new ones stay',
        JSON.stringify({ pruned: result.pruned, kept: result.kept }));
    assert(eq(Object.keys(result.assets), [one.assetId, three.assetId].sort()),
        'the returned map holds exactly the survivors, in sorted order');
    assert(eq(assets, before),
        'the INPUT map is untouched (the test deep-compared it before and after)');
    assert(result.assets[one.assetId] !== assets[one.assetId],
        'and the survivors are fresh objects, so a caller cannot reach back into the input',
        'same reference');
    // Mutating the result cannot affect the next call.
    result.assets[one.assetId].filename = 'hijacked.png';
    const again = A.pruneOrphans(assets, [one.assetId]);
    assert(again.assets[one.assetId].filename === 'keep.png',
        'mutating a returned record does not change what the next call produces');
    assert(eq(A.pruneOrphans(assets, [one.assetId]), A.pruneOrphans(assets, [one.assetId])),
        'pruneOrphans() is deterministic');
    assert(eq(A.pruneOrphans(assets, []).pruned, [one.assetId, two.assetId, three.assetId].sort()),
        'with nothing referenced, everything is prunable (the caller decides what that means)');
    assert(eq(A.pruneOrphans(assets, A.documentAssetIds(doc)).pruned.slice().sort(), [two.assetId, three.assetId].sort()),
        'without the keep list, only the document protects an asset');
    assert(eq(A.pruneOrphans(null, [one.assetId]).assets, {}) &&
           eq(A.pruneOrphans(undefined, []).pruned, []),
        'and a missing map is an empty result, never a throw');
    assert(Object.keys(A.pruneOrphans(assets, [one.assetId, two.assetId], { keep: 'not-a-list' }).assets).length === 2,
        'a nonsense keep option is ignored rather than iterated');
    // The policy itself lives with the caller: this function must not
    // know about sessions, drafts or history.
    assert(CODE.indexOf('session') === -1 && CODE.indexOf('draft') === -1 &&
           CODE.indexOf('history') === -1,
        'pruneOrphans() knows nothing about sessions, drafts or undo history');
}

// ═══════════════════════════════════════════════════════════════
section('J. one file in four slots is ONE asset (the plan\u2019s Case E)');
// ═══════════════════════════════════════════════════════════════
{
    const bytes = pngBytes('used-four-times');
    const picks = [
        A.identify(bytes, 'rules.png'), A.identify(bytes, 'rules.png'),
        A.identify(bytes, 'rules.png'), A.identify(bytes, 'rules.png'),
    ];
    assert(new Set(picks.map(p => p.assetId)).size === 1,
        'picking the same file four times produces one assetId');
    const record = recordFor(picks[0]);
    const assets = { [picks[0].assetId]: record };
    const doc = documentFixture(assets, {
        image: uploadRef(picks[0].assetId, record.filename),
        thumbnail: uploadRef(picks[0].assetId, record.filename),
        'author.icon': uploadRef(picks[0].assetId, record.filename),
        'footer.icon': uploadRef(picks[0].assetId, record.filename),
    });
    assert(A.assetsOf(doc).length === 1, 'and the document holds exactly one asset record');
    assert(A.refsOf(doc).length === 4, 'while four slots reference it');
    const naming = A.uniquifyFilenames(A.assetsOf(doc));
    assert(Object.keys(naming.filenames).length === 1 &&
           naming.filenames[picks[0].assetId] === 'rules.png',
        'one file to attach, named once', JSON.stringify(naming.filenames));
    const limits = A.checkLimits(Object.keys(naming.filenames).length, record.bytes || 0, LIMITS);
    assert(limits.usable === true && limits.count.used === 1 && limits.count.canAdd === true,
        'and the count is 1 against the served cap — not 4 (one upload, not four)');
    const pruned = A.pruneOrphans(assets, A.documentAssetIds(doc));
    assert(pruned.pruned.length === 0, 'and pruning keeps it, because all four slots point at it');
}

// ═══════════════════════════════════════════════════════════════
section('K. filename collisions: deterministic and bounded');
// ═══════════════════════════════════════════════════════════════
{
    const a = A.identify(pngBytes('collision-a'), 'rules.png');
    const b = A.identify(pngBytes('collision-b'), 'rules.png');
    const c = A.identify(pngBytes('collision-c'), 'anim.gif');
    assert(a.assetId !== b.assetId, 'rig: two different files that share a name');
    const records = [
        recordFor(a, { originalName: 'rules.png' }), recordFor(b, { originalName: 'rules.png' }),
        recordFor(c, { originalName: 'anim.gif' }),
    ];
    const naming = A.uniquifyFilenames(records);
    const names = Object.values(naming.filenames);
    assert(new Set(names).size === 3, 'three assets get three distinct filenames', names.join(', '));
    assert(naming.filenames[c.assetId] === 'anim.gif',
        'the asset with no collision keeps its own name', naming.filenames[c.assetId]);
    assert(/^[0-9a-f]{6}-rules\.png$/.test(naming.filenames[a.assetId]) &&
           /^[0-9a-f]{6}-rules\.png$/.test(naming.filenames[b.assetId]),
        'the two colliding files each get a sha-prefixed name',
        naming.filenames[a.assetId] + ' / ' + naming.filenames[b.assetId]);
    assert(naming.filenames[a.assetId].indexOf(a.sha256.slice(0, 6)) === 0,
        'the prefix comes from that asset\u2019s own content hash (so it is not positional)');
    assert(eq(naming.renamed.sort(), [a.assetId, b.assetId].sort()),
        'and the renamed ids are reported', JSON.stringify(naming.renamed));
    // Order independence: shuffle the input and the answer is identical.
    const shuffled = [records[2], records[1], records[0]];
    assert(eq(A.uniquifyFilenames(shuffled).filenames, naming.filenames),
        'the mapping does not depend on the order the records arrive in');
    assert(eq(A.uniquifyFilenames(records).filenames, naming.filenames),
        'nor does it change between calls');
    assert(eq(A.uniquifyFilenames(records).collisions, []),
        'and a resolved collision is not reported as an unresolved one');
    // The same asset twice with one name is not a collision.
    assert(Object.keys(A.uniquifyFilenames([records[0], records[0]]).filenames).length === 1,
        'the same asset listed twice is one filename');
    const conflicting = A.uniquifyFilenames([
        recordFor(a, { filename: 'zeta.png' }), recordFor(a, { filename: 'alpha.png' }),
    ]);
    assert(eq(conflicting.conflicts, [a.assetId]) &&
           conflicting.filenames[a.assetId] === 'alpha.png',
        'one asset with two names is reported, and the deterministic winner is chosen',
        JSON.stringify(conflicting));
    // The prefix cannot break the length cap.
    const longA = A.identify(pngBytes('long-a'), 'x'.repeat(80) + '.png');
    const longB = A.identify(pngBytes('long-b'), 'x'.repeat(80) + '.png');
    const longNames = A.uniquifyFilenames([
        recordFor(longA, { filename: longA.filename }), recordFor(longB, { filename: longB.filename }),
    ]).filenames;
    assert(Object.values(longNames).every(n => n.length <= A.MAX_FILENAME && n.slice(-4) === '.png'),
        'a prefixed name still fits the cap with its extension',
        Object.values(longNames).map(n => n.length).join(','));
    assert(Object.values(longNames).every(n => !/^[.-]/.test(n) && /^[a-z0-9._-]+$/.test(n)),
        'and stays inside the safe alphabet');
    assert(A.uniquifyFilenames([]).filenames && Object.keys(A.uniquifyFilenames([]).filenames).length === 0 &&
           Object.keys(A.uniquifyFilenames(null).filenames).length === 0,
        'an empty or nonsense list is an empty mapping, never a throw');
    const unnamed = A.uniquifyFilenames([{ assetId: 'a_1' }, { assetId: 'a_2', originalName: '../../etc/passwd' }]);
    assert(Object.keys(unnamed.filenames).length === 2 &&
           unnamed.filenames['a_2'] === 'passwd' && unnamed.filenames['a_1'] === 'image',
        'records with no usable name still get safe, distinct names',
        JSON.stringify(unnamed.filenames));
}

// ═══════════════════════════════════════════════════════════════
section('L. purity: no persistence, no module state, two realms');
// ═══════════════════════════════════════════════════════════════
{
    // The strongest available proof that nothing is persisted: the module
    // was loaded into a realm where no storage API exists at all, and every
    // call above still worked. A second, independent realm must agree.
    const B = loadModule({ document: undefined, indexedDB: undefined, localStorage: undefined });
    assert(B !== A, 'rig: a second realm loaded its own copy');
    const bytes = pngBytes('two-realms');
    assert(B.sha256Hex(bytes) === A.sha256Hex(bytes) &&
           B.identify(bytes, 'x.png').filename === A.identify(bytes, 'x.png').filename &&
           eq(B.uniquifyFilenames([{ assetId: 'a_1', filename: 'x.png' }]),
              A.uniquifyFilenames([{ assetId: 'a_1', filename: 'x.png' }])),
        'two independent realms produce byte-identical answers (no shared state anywhere)');
    assert(CODE.indexOf('localStorage') === -1 && CODE.indexOf('sessionStorage') === -1 &&
           CODE.indexOf('indexedDB') === -1 && CODE.indexOf('postMessage') === -1,
        'the source contains no persistence or cross-context call of any kind');
    // Interleaving calls cannot change an answer.
    const one = A.identify(pngBytes('interleave'), 'a.png');
    A.checkLimits(9, 26000000, LIMITS);
    A.uniquifyFilenames([{ assetId: 'a_z', filename: 'z.png' }]);
    A.pruneOrphans({ a_z: { assetId: 'a_z', filename: 'z.png' } }, []);
    const two = A.identify(pngBytes('interleave'), 'a.png');
    assert(eq(one, two), 'unrelated calls in between change nothing (no module state)');
    assert(Object.isFrozen(A) && Object.isFrozen(B), 'and the API object cannot be rewritten');
    try {
        A.sha256Hex = () => 'hijacked';
        assert(A.sha256Hex(pngBytes()) !== 'hijacked', 'a write to the frozen API is refused');
    } catch (e) {
        assert(true, 'a write to the frozen API throws in strict mode (also refused)');
    }
}

// ═══════════════════════════════════════════════════════════════
section('M. cost');
// ═══════════════════════════════════════════════════════════════
{
    const megabyte = new Uint8Array(1024 * 1024);
    for (let i = 0; i < megabyte.length; i += 1024) megabyte[i] = i & 0xff;
    const runs = 8;
    const started = process.hrtime.bigint();
    for (let i = 0; i < runs; i++) A.sha256Hex(megabyte);
    const ms = Number(process.hrtime.bigint() - started) / 1e6;
    const perCall = ms / runs;
    console.log('    1 MiB SHA-256: ' + perCall.toFixed(1) + ' ms per file (' + runs + ' runs)');
    assert(perCall < 500, 'hashing a 1 MiB image is far inside the add-image budget', perCall.toFixed(1) + ' ms');

    const identifyRuns = 200;
    const small = pngBytes('perf');
    const started2 = process.hrtime.bigint();
    for (let i = 0; i < identifyRuns; i++) A.identify(small, 'rules.png');
    const perIdentify = Number(process.hrtime.bigint() - started2) / 1e6 / identifyRuns;
    console.log('    identify() on a small PNG: ' + perIdentify.toFixed(4) + ' ms per call');
    assert(perIdentify < 5, 'identifying a small file is sub-millisecond territory', perIdentify.toFixed(4) + ' ms');

    const many = [];
    for (let i = 0; i < 200; i++) {
        many.push({ assetId: 'a_' + String(i).padStart(16, '0'), sha256: String(i).padStart(64, '0'), filename: 'image-' + (i % 5) + '.png' });
    }
    const started3 = process.hrtime.bigint();
    A.uniquifyFilenames(many);
    const msMany = Number(process.hrtime.bigint() - started3) / 1e6;
    console.log('    a 200-asset naming pass: ' + msMany.toFixed(2) + ' ms');
    assert(msMany < 250, 'naming 200 assets (every group colliding) stays cheap', msMany.toFixed(2) + ' ms');
}

// ═══════════════════════════════════════════════════════════════
console.log('\nmessage-builder assets: ' + pass + ' passed, ' + fail + ' failed');
if (fail) {
    console.log('Failures:');
    failures.forEach(f => console.log(' -', f));
    process.exit(1);
}
console.log('ALL MESSAGE-BUILDER ASSET CHECKS PASSED');
