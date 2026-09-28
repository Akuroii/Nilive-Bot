/* ═══════════════════════════════════════════════════════════════
   embed/assets.js — phase 2, step 7a: the asset CORE (pure).

   WHAT AN ASSET IS
   An image the user picked, identified by its CONTENT, not by its
   name or its URL:

     a_<sha256[0:16]>          content-addressed, so the same file used
                               four times is one asset and uploads once

   The document never holds bytes. It holds a reference per slot
   (`{kind:'upload', assetId, filename, …}` — modelled in phase 1 by
   model.js's mediaFromValue/mediaToWireUrl) and this module owns the
   rules about what those references may point at.

   WHAT THIS FILE IS (AND IS NOT)
   This is the PURE half of the asset layer: identity, sniffing,
   filenames, limits arithmetic, the record shape, and the read-only
   document queries (assetsOf / refsOf / pruneOrphans). It has no
   clock, no randomness, no DOM, no storage, no network and no module
   state — every answer is a function of its arguments, which is what
   makes it testable and what lets the mutation battery judge it.

   The OTHER half (phase 2, step 7b+) is deliberately elsewhere:
     * reading a File/Blob into bytes        → the page/asset store
     * IndexedDB bytes                       → the existing drafts.js
                                               adapter (one persistence
                                               boundary in this codebase)
     * object URLs for the preview           → the asset store, revoked
                                               on drop/teardown
     * the files summary / "Send"            → the page (phase 2B/5)

   LIMITS: ONE TABLE, NO LITERALS
   Every number comes from the served payload
   (`utils/discord_limits.limits_payload()["attachments"]`), read
   through the keys listed in LIMIT_KEYS — the same list
   scripts/test_embed_schema.py checks against the server, so a
   counter, a rule and the send gate can never disagree about what
   "too many files" means. A missing or unusable table makes every
   answer NULL, never a guess: fail closed, never "unlimited".

   DELIBERATE AND DOCUMENTED CHOICES
     * jpg and jpeg are the same format, so `photo.jpeg` with JPEG
       bytes is legal; the declared alias is KEPT (it is the name the
       user saw) rather than rewritten to `jpg`.
     * The extension is the only thing a name may claim; the bytes
       decide. A name that says .png over JPEG bytes is REFUSED with
       the reason, never silently renamed: a renamed file is how a
       user ends up uploading something they did not choose.
     * `availability` may only be 'bytes-local' or 'bytes-missing'.
       Anything else is normalised to 'bytes-missing' — an unknown
       state must never claim we hold the bytes.
     * Two different assets whose names collide get a deterministic
       `<sha6>-` prefix (both members, so the result is independent of
       input order); the prefix grows only if that prefix itself
       collides.
     * `width`/`height` are part of the record shape but stay null in
       this step: decoding dimensions is a property of reading the
       bytes, which belongs with the byte store (7b). The shape does
       not change when they start being filled in.

   Tested by: scripts/test_message_builder_assets.js
   ═══════════════════════════════════════════════════════════════ */
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    // ── The formats an embed image slot accepts ───────────────────
    const MIME_PNG = 'image/png';
    const MIME_JPEG = 'image/jpeg';
    const MIME_WEBP = 'image/webp';
    const MIME_GIF = 'image/gif';

    const EXTENSION_BY_MIME = Object.freeze({
        'image/png': 'png',
        'image/jpeg': 'jpg',
        'image/webp': 'webp',
        'image/gif': 'gif',
    });
    // Every spelling the allow-list accepts; jpg/jpeg are one format.
    const MIME_BY_EXTENSION = Object.freeze({
        gif: MIME_GIF,
        jpeg: MIME_JPEG,
        jpg: MIME_JPEG,
        png: MIME_PNG,
        webp: MIME_WEBP,
    });
    const ALLOWED_EXTENSIONS = Object.freeze(['gif', 'jpeg', 'jpg', 'png', 'webp']);

    /** The longest filename this module will ever produce. */
    const MAX_FILENAME = 64;
    /** Fallback stem when a name has nothing usable left in it. */
    const FALLBACK_STEM = 'image';
    /** How much of the hash to show in a de-collision prefix (grows only
     *  when that prefix is itself ambiguous). */
    const PREFIX_LENGTHS = [6, 8, 16];

    const RECORD_KEYS = Object.freeze([
        'assetId', 'sha256', 'mime', 'bytes', 'width', 'height',
        'originalName', 'filename', 'availability', 'createdAt',
    ]);
    const AVAILABILITY = ['bytes-local', 'bytes-missing'];

    /**
     * The exact server keys this module reads. `required` is the pair
     * without which nothing can be measured at all; the advisory pair
     * is optional (an absent advisory means "no advisory", never a
     * guessed byte count).
     */
    const LIMIT_KEYS = Object.freeze({
        required: Object.freeze([
            Object.freeze(['attachments', 'count_max']),
            Object.freeze(['attachments', 'total_bytes_max']),
        ]),
        advisory: Object.freeze([
            Object.freeze(['attachments', 'file_bytes_advisory']),
            Object.freeze(['attachments', 'file_advisory_is_hard']),
        ]),
    });

    // ── Bytes ─────────────────────────────────────────────────────
    /**
     * Uint8Array | ArrayBuffer | Array of byte values → Uint8Array
     * (or null). A File/Blob is deliberately NOT accepted here: reading
     * one is asynchronous, and this module answers in the same tick
     * (the byte store in 7b does the reading).
     *
     * The checks are structural (`ArrayBuffer.isView`, the toString
     * tag) rather than `instanceof`, because a Uint8Array created in
     * another realm — a test sandbox, an iframe, a Worker's response —
     * fails `instanceof` while being exactly the same bytes. Every
     * view is re-wrapped in this realm so nothing here reads through a
     * foreign object, and a view's byteOffset/byteLength are respected
     * (Node's Buffer is a view into a shared pool and must not be
     * hashed whole).
     */
    function toBytes(value) {
        if (!value) return null;
        if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView(value)) {
            const view = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
            return view;
        }
        if (typeof ArrayBuffer !== 'undefined' &&
            Object.prototype.toString.call(value) === '[object ArrayBuffer]') {
            return new Uint8Array(value);
        }
        if (Array.isArray(value)) {
            for (let i = 0; i < value.length; i++) {
                if (typeof value[i] !== 'number') return null;
            }
            return new Uint8Array(value);
        }
        return null;
    }

    function startsWith(bytes, signature) {
        if (!bytes || bytes.length < signature.length) return false;
        for (let i = 0; i < signature.length; i++) {
            if (bytes[i] !== signature[i]) return false;
        }
        return true;
    }

    function ascii(bytes, at, length) {
        let out = '';
        for (let i = 0; i < length; i++) {
            const b = bytes[at + i];
            if (b === undefined) return out;
            out += String.fromCharCode(b);
        }
        return out;
    }

    const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
    const JPEG_SIGNATURE = [0xff, 0xd8, 0xff];
    const GIF_SIGNATURE = [0x47, 0x49, 0x46, 0x38];
    const RIFF_SIGNATURE = [0x52, 0x49, 0x46, 0x46];

    /**
     * What the BYTES say this is: 'image/png' | 'image/jpeg' |
     * 'image/webp' | 'image/gif' | null. Content wins over the name
     * everywhere in this module.
     */
    function sniffMime(value) {
        const bytes = toBytes(value);
        if (!bytes) return null;
        if (startsWith(bytes, PNG_SIGNATURE)) return MIME_PNG;
        if (startsWith(bytes, JPEG_SIGNATURE)) return MIME_JPEG;
        if (startsWith(bytes, GIF_SIGNATURE) &&
            (bytes[4] === 0x37 || bytes[4] === 0x39) && bytes[5] === 0x61) {
            return MIME_GIF;
        }
        if (startsWith(bytes, RIFF_SIGNATURE) && ascii(bytes, 8, 4) === 'WEBP') {
            return MIME_WEBP;
        }
        return null;
    }

    /** The human name of the format that was detected (or the sniffed one). */
    function mimeLabel(mime) {
        if (mime === MIME_PNG) return 'PNG';
        if (mime === MIME_JPEG) return 'JPEG';
        if (mime === MIME_WEBP) return 'WebP';
        if (mime === MIME_GIF) return 'GIF';
        return 'unknown';
    }

    /**
     * A short, honest description of bytes that are NOT an accepted
     * format, so the refusal can say what the user actually picked.
     * Only ever called on the failure path.
     */
    function describeBytes(value) {
        const bytes = toBytes(value);
        if (!bytes || !bytes.length) return 'an empty file';
        if (ascii(bytes, 0, 5) === '%PDF-') return 'a PDF document';
        if (startsWith(bytes, [0x50, 0x4b, 0x03, 0x04])) return 'a ZIP archive';
        if (startsWith(bytes, [0x42, 0x4d])) return 'a bitmap image';
        if (startsWith(bytes, [0x00, 0x00, 0x01, 0x00])) return 'an icon file';
        const head = ascii(bytes, 0, Math.min(64, bytes.length));
        if (/^\s*(<\?xml|<svg)/i.test(head)) return 'an SVG (Discord does not render those in embeds)';
        if (/^[\x09\x0a\x0d\x20-\x7e]*$/.test(head)) return 'text, not an image';
        return 'not a recognised image';
    }

    /**
     * Bytes as a short human string: 900 → '900 bytes', 1536 → '1.5 KB'.
     * The thresholds are about READABILITY, not about any limit — nothing in
     * this function decides whether a file is acceptable (that is
     * checkFileSize(), against the served table).
     */
    function describeSize(value) {
        const bytes = (typeof value === 'number' && isFinite(value) && value > 0) ? Math.round(value) : 0;
        if (bytes < 1024) return bytes + (bytes === 1 ? ' byte' : ' bytes');
        const kb = bytes / 1024;
        if (kb < 1024) return (Math.round(kb * 10) / 10) + ' KB';
        return (Math.round((kb / 1024) * 10) / 10) + ' MB';
    }

    // ── SHA-256 (pure, synchronous, dependency-free) ──────────────
    /**
     * A synchronous SHA-256 over bytes. `crypto.subtle` is async and
     * secure-context-only; identity has to be answerable in the same
     * tick as the file picker event, so the 64 lines live here rather
     * than an async dependency. Verified against node's crypto in
     * scripts/test_message_builder_assets.js (empty, short, the 55/56
     * and 64-byte padding edges, and multi-block inputs).
     */
    const SHA_K = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ];

    function rotr(value, bits) {
        return ((value >>> bits) | (value << (32 - bits))) >>> 0;
    }

    /** Lowercase hex SHA-256 of the given bytes ('' for unusable input). */
    function sha256Hex(value) {
        const bytes = toBytes(value);
        if (!bytes) return '';
        const H = [
            0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
            0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
        ];
        const length = bytes.length;
        const afterOne = length + 1;
        const padding = ((56 - (afterOne % 64)) + 64) % 64;
        const total = afterOne + padding + 8;
        const buffer = new Uint8Array(total);
        buffer.set(bytes);
        buffer[length] = 0x80;
        // Bit length, 64-bit big-endian, without 32-bit overflow.
        const bitsHigh = Math.floor((length * 8) / 4294967296);
        const bitsLow = (length * 8) >>> 0;
        buffer[total - 8] = (bitsHigh >>> 24) & 0xff;
        buffer[total - 7] = (bitsHigh >>> 16) & 0xff;
        buffer[total - 6] = (bitsHigh >>> 8) & 0xff;
        buffer[total - 5] = bitsHigh & 0xff;
        buffer[total - 4] = (bitsLow >>> 24) & 0xff;
        buffer[total - 3] = (bitsLow >>> 16) & 0xff;
        buffer[total - 2] = (bitsLow >>> 8) & 0xff;
        buffer[total - 1] = bitsLow & 0xff;

        const w = new Uint32Array(64);
        for (let offset = 0; offset < total; offset += 64) {
            for (let i = 0; i < 16; i++) {
                const at = offset + i * 4;
                w[i] = ((buffer[at] << 24) | (buffer[at + 1] << 16) |
                        (buffer[at + 2] << 8) | buffer[at + 3]) >>> 0;
            }
            for (let i = 16; i < 64; i++) {
                const x = w[i - 15];
                const y = w[i - 2];
                const s0 = (rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3)) >>> 0;
                const s1 = (rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10)) >>> 0;
                w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
            }
            let a = H[0], b = H[1], c = H[2], d = H[3];
            let e = H[4], f = H[5], g = H[6], h = H[7];
            for (let i = 0; i < 64; i++) {
                const S1 = (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) >>> 0;
                const ch = ((e & f) ^ (~e & g)) >>> 0;
                const t1 = (h + S1 + ch + SHA_K[i] + w[i]) >>> 0;
                const S0 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) >>> 0;
                const maj = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
                const t2 = (S0 + maj) >>> 0;
                h = g; g = f; f = e; e = (d + t1) >>> 0;
                d = c; c = b; b = a; a = (t1 + t2) >>> 0;
            }
            H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0;
            H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
            H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0;
            H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
        }
        let hex = '';
        for (let i = 0; i < 8; i++) hex += H[i].toString(16).padStart(8, '0');
        return hex;
    }

    /** The content-addressed id for a hex digest. */
    function assetIdFromSha(sha) {
        const hex = String(sha || '').toLowerCase();
        if (!/^[0-9a-f]{16,}$/.test(hex)) return '';
        return 'a_' + hex.slice(0, 16);
    }

    // ── Filenames ─────────────────────────────────────────────────
    /** Last path component, whichever separator the OS used. */
    function baseName(name) {
        const raw = String(name == null ? '' : name);
        const parts = raw.split(/[\\/]/);
        return parts[parts.length - 1];
    }

    /**
     * The extension a NAME claims, lowercased: 'Rules.PNG' → 'png'.
     * A leading dot is not an extension ('.png' has none — it is a
     * hidden file, and treating it as one would let a dotfile pretend
     * to be an image), and a dotted stem is not either ('..png').
     */
    function filenameExtension(name) {
        const base = baseName(name).toLowerCase();
        const dot = base.lastIndexOf('.');
        if (dot <= 0 || dot === base.length - 1) return '';
        return base.slice(dot + 1);
    }

    /** The part of a name before its extension (after sanitising). */
    function sanitiseStem(name) {
        const base = baseName(name).toLowerCase().replace(/\.[^.]*$/, function (tail, at) {
            // Only strip a trailing extension when the dot is not the
            // first character (a leading dot is a hidden file's marker).
            return at === 0 ? tail : '';
        });
        let stem = base
            .replace(/[^a-z0-9._-]+/g, '-')   // anything else becomes a dash
            .replace(/-{2,}/g, '-')           // collapse runs
            .replace(/^[.-]+/, '')            // never a dotfile, never a leading dash
            .replace(/[.-]+$/, '');           // no trailing punctuation before the dot
        if (!stem) stem = FALLBACK_STEM;
        return stem;
    }

    /** The bounded stem for a given extension (keeps the extension intact). */
    function boundStem(stem, extension) {
        const room = MAX_FILENAME - (extension ? extension.length + 1 : 0);
        if (stem.length <= room) return stem;
        const cut = stem.slice(0, Math.max(1, room)).replace(/[.-]+$/, '');
        return cut || FALLBACK_STEM;
    }

    function joinName(stem, extension) {
        return extension ? stem + '.' + extension : stem;
    }

    /**
     * A safe filename: lowercase, no directories, only [a-z0-9._-],
     * no leading dot/dash, at most MAX_FILENAME characters. Never
     * empty, never a path, never a dotfile.
     */
    function sanitiseFilename(name) {
        const extension = filenameExtension(name);
        const safeExt = /^[a-z0-9]{1,8}$/.test(extension) ? extension : '';
        return joinName(boundStem(sanitiseStem(name), safeExt), safeExt);
    }

    /** `<sha6>-name.png`, re-bounded so the prefix cannot break the cap. */
    function prefixedFilename(record, prefixLength) {
        const sha = String((record && record.sha256) || '').toLowerCase();
        const fallback = String((record && record.assetId) || '').replace(/^a_/, '').toLowerCase();
        const source = /^[0-9a-f]+$/.test(sha) ? sha : fallback;
        const prefix = source.slice(0, prefixLength).replace(/[^0-9a-f]/g, '') || 'x';
        const extension = filenameExtension(record.filename);
        const stem = sanitiseStem(record.filename);
        const room = MAX_FILENAME - (prefix.length + 1) - (extension ? extension.length + 1 : 0);
        const bounded = stem.length <= room ? stem : (stem.slice(0, Math.max(1, room)).replace(/[.-]+$/, '') || FALLBACK_STEM);
        return prefix + '-' + joinName(bounded, extension);
    }

    /**
     * One filename per DISTINCT assetId, deterministic and independent
     * of input order:
     *
     *   * an asset's own name is used when nothing else claims it;
     *   * every member of a colliding group gets a `<shaN>-` prefix
     *     (all of them, so the answer cannot depend on which record
     *     was seen first);
     *   * if that prefix itself collides, it grows (6 → 8 → 16 hex
     *     characters, i.e. the full id) — the loop always terminates.
     *
     * Two entries for ONE assetId are a caller error: the
     * lexicographically smallest filename wins (deterministic) and the
     * assetId is reported in `conflicts` so the caller can surface it.
     */
    function uniquifyFilenames(records) {
        const list = Array.isArray(records) ? records : [];
        const byId = {};
        const conflicts = [];
        list.forEach(record => {
            if (!record || !record.assetId) return;
            const id = String(record.assetId);
            const filename = sanitiseFilename(record.filename || record.originalName || '');
            const entry = { assetId: id, sha256: record.sha256 || '', filename: filename };
            if (!byId[id]) {
                byId[id] = entry;
            } else if (byId[id].filename !== filename) {
                if (conflicts.indexOf(id) === -1) conflicts.push(id);
                if (filename < byId[id].filename) byId[id] = entry;
            }
        });

        const ids = Object.keys(byId).sort();
        let names = {};
        ids.forEach(id => { names[id] = byId[id].filename; });

        const named = {};
        ids.forEach(id => { named[id] = names[id]; });

        let collisions = [];
        PREFIX_LENGTHS.forEach(function (prefixLength, index) {
            const groups = {};
            ids.forEach(id => {
                const name = names[id];
                (groups[name] = groups[name] || []).push(id);
            });
            collisions = Object.keys(groups).filter(name => groups[name].length > 1).sort();
            if (!collisions.length) return;
            const last = index === PREFIX_LENGTHS.length - 1;
            collisions.forEach(name => {
                groups[name].forEach(id => {
                    names[id] = last
                        ? prefixedFilename(byId[id], PREFIX_LENGTHS[PREFIX_LENGTHS.length - 1])
                        : prefixedFilename(byId[id], PREFIX_LENGTHS[index]);
                });
            });
        });

        // Sorted keys, so the returned map serialises deterministically.
        const ordered = {};
        const renamed = [];
        ids.forEach(id => {
            ordered[id] = names[id];
            if (names[id] !== named[id]) renamed.push(id);
        });
        return {
            ok: true, filenames: ordered,
            collisions: collisions, renamed: renamed.sort(), conflicts: conflicts.sort(),
        };
    }

    // ── Limits (the served table is the only source) ──────────────
    /** Is the served attachments table usable? Never guesses. */
    function attachmentLimits(limits) {
        const table = limits && limits.attachments;
        const missing = [];
        LIMIT_KEYS.required.forEach(([block, key]) => {
            const value = table && table[key];
            const ok = typeof value === 'number' && isFinite(value) && value >= 0;
            if (!ok) missing.push(block + '.' + key);
        });
        return { ok: missing.length === 0, reason: missing.length ? 'limits-unusable' : null, missing: missing };
    }

    /** The advisory pair, or null when the table does not carry it. */
    function sizeAdvisory(limits) {
        const table = (limits && limits.attachments) || {};
        const value = table.file_bytes_advisory;
        if (typeof value !== 'number' || !isFinite(value) || value <= 0) return null;
        return { bytes: value, isHard: table.file_advisory_is_hard === true };
    }

    /**
     * Count/bytes arithmetic against the served table, in the same
     * `{used, max, over}` shape the 6b counters use, so a rail counter
     * and a validation rule can be painted from one answer.
     * Unusable table → every number is NULL (nothing to measure with).
     */
    function checkLimits(count, totalBytes, limits) {
        const usable = attachmentLimits(limits);
        if (!usable.ok) {
            return {
                usable: false, reason: usable.reason, missing: usable.missing,
                count: null, bytes: null, advisory: null,
            };
        }
        const used = typeof count === 'number' && count > 0 ? Math.trunc(count) : 0;
        const size = typeof totalBytes === 'number' && totalBytes > 0 ? totalBytes : 0;
        // Reached only when attachmentLimits() said the two required keys
        // are present; reading them through a guarded table means a
        // malformed argument can never throw here (a module that crashes
        // on bad input cannot report the bad input).
        const table = (limits && limits.attachments) || {};
        const countMax = table.count_max;
        const bytesMax = table.total_bytes_max;
        return {
            usable: true,
            reason: null,
            missing: [],
            count: { used: used, max: countMax, over: used > countMax, canAdd: used < countMax },
            bytes: { used: size, max: bytesMax, over: size > bytesMax },
            advisory: sizeAdvisory(limits),
        };
    }

    /**
     * One file's size against the served per-file cap. Takes bytes OR a
     * byte count (a caller that already has `file.size` should not have
     * to allocate). The cap is ADVISORY unless the server says
     * otherwise (`file_advisory_is_hard`), because only Discord knows
     * the guild's real ceiling — a guess here would refuse a file
     * Discord accepts. No table → nulls, so nothing reads as "fine".
     */
    function checkFileSize(bytes, limits) {
        const size = typeof bytes === 'number' && isFinite(bytes) && bytes >= 0
            ? bytes
            : (toBytes(bytes) ? toBytes(bytes).length : null);
        const advisory = sizeAdvisory(limits);
        if (size === null) {
            return { usable: false, size: null, advisoryMax: null, oversized: null, blocked: null, isHard: null };
        }
        if (!advisory) {
            return { usable: false, size: size, advisoryMax: null, oversized: null, blocked: null, isHard: null };
        }
        const oversized = size > advisory.bytes;
        return {
            usable: true,
            size: size,
            advisoryMax: advisory.bytes,
            oversized: oversized,
            blocked: oversized && advisory.isHard,
            isHard: advisory.isHard,
        };
    }

    // ── Records ───────────────────────────────────────────────────
    /**
     * The asset record. Fixed key order (it is serialised into the
     * draft, and a stable order is what keeps the document hash
     * stable), everything the later steps need, and nothing that only
     * exists at runtime (no Blob, no URL).
     *
     * Required: assetId and a filename. `sha256`/`mime`/`bytes` are
     * filled from `identify()` in the normal flow; a record built by
     * hand without them is still shaped, never guessed.
     */
    function buildRecord(fields) {
        fields = fields || {};
        const missing = [];
        if (!fields.assetId) missing.push('assetId');
        const filename = fields.filename ? sanitiseFilename(fields.filename) : '';
        if (!filename) missing.push('filename');
        if (missing.length) return { ok: false, reason: 'incomplete-record', missing: missing, record: null };
        const availability = AVAILABILITY.indexOf(fields.availability) !== -1
            ? fields.availability : 'bytes-missing';
        return {
            ok: true,
            reason: null,
            missing: [],
            record: {
                assetId: String(fields.assetId),
                sha256: fields.sha256 ? String(fields.sha256).toLowerCase() : '',
                mime: fields.mime ? String(fields.mime) : '',
                bytes: typeof fields.bytes === 'number' && fields.bytes >= 0 ? fields.bytes : null,
                width: typeof fields.width === 'number' ? fields.width : null,
                height: typeof fields.height === 'number' ? fields.height : null,
                originalName: fields.originalName == null ? '' : String(fields.originalName),
                filename: filename,
                availability: availability,
                createdAt: fields.createdAt == null ? null : String(fields.createdAt),
            },
        };
    }

    /**
     * Is this a value a DRAFT can hold? The document is JSON: it is
     * hashed, serialized and compared for dirty-ness, so a Blob, a
     * typed array, a nested object or a function inside a record would
     * either silently change shape on the way to storage or make
     * drafts.assertSerializable() refuse the write outright. Only
     * strings, finite numbers, booleans and null pass.
     */
    function jsonScalar(value) {
        const type = typeof value;
        if (value === null) return true;
        if (type === 'string' || type === 'boolean') return true;
        return type === 'number' && isFinite(value);
    }

    /**
     * Keys a record carries that this build does not know AND that
     * cannot survive JSON, sorted. Forward compatibility is for DATA
     * from a newer phase — never for bytes smuggled into a document.
     */
    function foreignUnsafeKeys(value) {
        if (!value || typeof value !== 'object') return [];
        return Object.keys(value)
            .filter((key) => RECORD_KEYS.indexOf(key) === -1 && !jsonScalar(value[key]))
            .sort();
    }

    /**
     * A stored asset record read back: the known keys in the fixed
     * order, then any key this build does not know about (sorted) —
     * a newer phase's field is preserved, never dropped by a rebuild.
     *
     * The canonical keys are the ones RECORD_KEYS lists, and they are
     * rebuilt by buildRecord() above: this function NEVER invents a
     * shape of its own (that is what keeps the record contract in one
     * place). An unknown key is preserved only when its value is a
     * JSON scalar; anything else makes the whole record unreadable,
     * which is reported rather than quietly stored.
     */
    function normalizeRecord(value) {
        if (!value || typeof value !== 'object' || !value.assetId) return null;
        const built = buildRecord(value);
        if (!built.ok) return null;
        const out = built.record;
        if (foreignUnsafeKeys(value).length) return null;   // bytes in a record are not a record
        Object.keys(value).sort().forEach(key => {
            if (RECORD_KEYS.indexOf(key) !== -1) return;
            out[key] = value[key];
        });
        return out;
    }

    /** The assets a document carries, sorted by id (deterministic). */
    function assetsOf(document_) {
        const map = (document_ && document_.assets) || null;
        if (!map || typeof map !== 'object') return [];
        return Object.keys(map).sort()
            .map(key => normalizeRecord(map[key]))
            .filter(record => !!record);
    }

    /**
     * Asset entries that could not be understood, with the reason:
     *   missing-record    the key holds nothing at all
     *   unreadable-record the value is not a record (no id, no name, …)
     *   non-json-value    the value carries something a document cannot
     *                     hold (bytes, a nested object, a function) —
     *                     named separately because "this cannot be saved"
     *                     is a different conversation from "this is malformed"
     */
    function assetIssues(document_) {
        const map = (document_ && document_.assets) || null;
        if (!map || typeof map !== 'object') return [];
        const out = [];
        Object.keys(map).sort().forEach(key => {
            const unsafe = foreignUnsafeKeys(map[key]);
            if (unsafe.length) {
                out.push({ assetId: key, reason: 'non-json-value', keys: unsafe });
                return;
            }
            if (!normalizeRecord(map[key])) {
                out.push({ assetId: key, reason: map[key] ? 'unreadable-record' : 'missing-record' });
            }
        });
        return out;
    }

    /** The four slots an image can live in, in a fixed order. */
    function mediaSlots(embed, index) {
        const base = 'embeds.' + index;
        return [
            { slot: 'image', path: base + '.image', value: embed && embed.image },
            { slot: 'thumbnail', path: base + '.thumbnail', value: embed && embed.thumbnail },
            { slot: 'author.icon', path: base + '.author.icon', value: embed && embed.author && embed.author.icon },
            { slot: 'footer.icon', path: base + '.footer.icon', value: embed && embed.footer && embed.footer.icon },
        ];
    }

    /**
     * Every upload reference in the document, in document order.
     * Read-only: it reports what the document says, it never rewrites
     * it. `url`-mode media and empty slots are simply not references.
     */
    function refsOf(document_) {
        const embeds = (document_ && Array.isArray(document_.embeds)) ? document_.embeds : [];
        const out = [];
        embeds.forEach((embed, index) => {
            mediaSlots(embed, index).forEach(entry => {
                const value = entry.value;
                if (!value || typeof value !== 'object' || value.kind !== 'upload') return;
                out.push({
                    assetId: value.assetId ? String(value.assetId) : '',
                    embedId: embed && embed.id ? String(embed.id) : '',
                    slot: entry.slot,
                    path: entry.path,
                    filename: value.filename ? String(value.filename) : '',
                });
            });
        });
        return out;
    }

    /** The distinct asset ids a set of references points at, sorted. */
    function referencedAssetIds(refs) {
        const seen = {};
        (Array.isArray(refs) ? refs : []).forEach(ref => {
            if (ref && ref.assetId) seen[String(ref.assetId)] = true;
        });
        return Object.keys(seen).sort();
    }

    /** The ids referenced by a document. */
    function documentAssetIds(document_) {
        return referencedAssetIds(refsOf(document_));
    }

    /**
     * Drop asset records nothing references. PURE and deterministic:
     * the input map is never touched, the returned map is new, and the
     * decision is exactly "is this id in the keep list".
     *
     * WHERE THE POLICY LIVES (phase 2 decision 4): the CALLER decides
     * what counts as referenced — the current document, the saved
     * snapshot, the last persisted record, and anything created during
     * this session (passed through `opts.keep`). This function only
     * performs the subtraction, so there is one refcount rule in the
     * codebase and no hidden second one here.
     */
    function pruneOrphans(assets, referencedIds, opts) {
        opts = opts || {};
        const keep = {};
        (Array.isArray(referencedIds) ? referencedIds : []).forEach(id => {
            if (id) keep[String(id)] = true;
        });
        (Array.isArray(opts.keep) ? opts.keep : []).forEach(id => {
            if (id) keep[String(id)] = true;
        });
        const source = (assets && typeof assets === 'object') ? assets : {};
        const out = {};
        const kept = [];
        const pruned = [];
        Object.keys(source).sort().forEach(key => {
            if (keep[key]) {
                out[key] = normalizeRecord(source[key]) || source[key];
                kept.push(key);
            } else {
                pruned.push(key);
            }
        });
        return { ok: true, assets: out, kept: kept, pruned: pruned };
    }

    // ── Identification (the one entry point for "here is a file") ──
    /**
     * bytes + the name the user saw → the facts everything else uses.
     *
     * Returns { ok, assetId, sha256, mime, ext, filename, reason,
     *           message, declaredExt }
     *
     * `ok:false` always carries a machine reason AND a sentence that
     * names the real problem — the caller shows it, the caller does
     * not invent its own wording:
     *   unrecognised-bytes     not a PNG/JPEG/WebP/GIF
     *   extension-not-allowed  the name claims a format an embed cannot show
     *   extension-mismatch     the name and the bytes disagree
     *   no-bytes               nothing to identify
     */
    function identify(value, declaredName) {
        const bytes = toBytes(value);
        const name = String(declaredName == null ? '' : declaredName);
        if (!bytes || !bytes.length) {
            return {
                ok: false, reason: 'no-bytes', declaredExt: filenameExtension(name),
                message: 'That file is empty, so there is nothing to use as an image.',
                assetId: '', sha256: '', mime: null, ext: '', filename: sanitiseFilename(name),
            };
        }
        const declaredExt = filenameExtension(name);
        const sha = sha256Hex(bytes);
        const mime = sniffMime(bytes);
        const filename = sanitiseFilename(name);
        const base = { assetId: assetIdFromSha(sha), sha256: sha, declaredExt: declaredExt, filename: filename };

        // The NAME is checked first, and only for "is this an extension an
        // embed can show at all": a .svg or .pdf is refused whatever its
        // bytes say, because the useful answer is the name the user can
        // change (silently renaming their file is how someone ends up
        // uploading something they did not choose).
        if (declaredExt && !Object.prototype.hasOwnProperty.call(MIME_BY_EXTENSION, declaredExt)) {
            return Object.assign(base, {
                ok: false, reason: 'extension-not-allowed', mime: mime, ext: declaredExt,
                message: 'The file name ends in .' + declaredExt +
                    '; embed images must be .jpg, .jpeg, .png, .webp or .gif.',
            });
        }
        // Then the BYTES decide the format — a .png name over a text file
        // is caught here, not accepted because the name looked right.
        if (!mime) {
            return Object.assign(base, {
                ok: false, reason: 'unrecognised-bytes', mime: null, ext: '',
                message: 'That file is ' + describeBytes(bytes) +
                    '. Embed images must be PNG, JPEG, WebP or GIF.',
            });
        }
        if (declaredExt && MIME_BY_EXTENSION[declaredExt] !== mime) {
            return Object.assign(base, {
                ok: false, reason: 'extension-mismatch', mime: mime, ext: declaredExt,
                message: 'The file name says .' + declaredExt + ' but the file itself is ' +
                    mimeLabel(mime) + '. Rename it or pick the right file.',
            });
        }
        // The declared alias wins when it is the same format (photo.jpeg
        // stays photo.jpeg); otherwise the sniffed format names it.
        const ext = declaredExt || EXTENSION_BY_MIME[mime];
        const finalName = joinName(boundStem(sanitiseStem(name), ext), ext);
        return {
            ok: true, reason: null, message: '',
            assetId: base.assetId, sha256: sha, mime: mime, ext: ext,
            declaredExt: declaredExt, filename: finalName,
        };
    }

    // ── Availability FACTS (phase 2, step 7c) ─────────────────────
    /**
     * A FACT is what the byte store OBSERVED for one asset. It is not
     * part of the document, it is never inferred from the document, and
     * it is never cached here: the page probes, the page hands the rows
     * in, and this module turns them into the one vocabulary the
     * validation rules speak.
     *
     * The five states are deliberately NOT collapsible. "We looked and
     * there are no bytes" is a different statement from "we could not
     * look" and from "we did not look at all", and each one produces
     * different output: a missing file is a warning the user can act on,
     * an unavailable store is a warning that says the check could not
     * run, and an unobserved fact says NOTHING — assuming either way is
     * how a page ends up telling someone their image is broken.
     *
     *   bytes-local        observed: the bytes are here (session memory or storage)
     *   bytes-missing      observed: nothing is stored under that id
     *   bytes-unavailable  observed: the store could not answer (degraded
     *                      storage, a failed read, a closed connection)
     *   bytes-corrupt      observed: something is there and it is not a
     *                      usable byte entry
     *   unknown            not observed (no row for this id at all)
     */
    const FACT_STATES = Object.freeze({
        LOCAL: 'bytes-local',
        MISSING: 'bytes-missing',
        UNAVAILABLE: 'bytes-unavailable',
        CORRUPT: 'bytes-corrupt',
        UNKNOWN: 'unknown',
    });

    /** The store's "nothing under this id" reasons — the ONLY ones a
     *  probe result may be read as a miss. */
    const MISS_REASONS = ['missing', 'missing-entry'];
    /** The store's "something is there, but not bytes" reason. */
    const CORRUPT_REASONS = ['corrupt'];

    /**
     * A probe row → one of FACT_STATES. FAIL CLOSED: only the reasons
     * above are read as an observation. Any other reason is
     * `bytes-unavailable` (we could not tell), and a row that claims
     * "not present" without saying why is `unknown` — a store that
     * cannot explain itself has not told us anything.
     */
    function factState(row) {
        if (!row || typeof row !== 'object') return FACT_STATES.UNKNOWN;
        if (row.present === true) return FACT_STATES.LOCAL;
        const reason = row.reason == null ? '' : String(row.reason);
        if (CORRUPT_REASONS.indexOf(reason) !== -1) return FACT_STATES.CORRUPT;
        if (MISS_REASONS.indexOf(reason) !== -1) return FACT_STATES.MISSING;
        if (reason) return FACT_STATES.UNAVAILABLE;
        return FACT_STATES.UNKNOWN;
    }

    /**
     * The facts for a document: every id it references, the state of
     * each, and the raw rows. `rows` is what the byte store's survey()
     * returned — an array of `{assetId, present, reason, …}` — or null
     * when nothing was probed.
     *
     * Two rules that matter more than they look:
     *   • a referenced id with NO row is `unknown`, never `missing`
     *     (an incomplete probe is not evidence of absence);
     *   • the first row for an id wins, so the answer is deterministic
     *     even if a caller hands the same id in twice.
     */
    function assetFacts(document_, rows) {
        const ids = documentAssetIds(document_);
        const list = Array.isArray(rows) ? rows : null;
        const byId = {};
        (list || []).forEach((row) => {
            if (!row || typeof row !== 'object') return;
            const id = row.assetId ? String(row.assetId) : '';
            if (!id || Object.prototype.hasOwnProperty.call(byId, id)) return;
            byId[id] = row;
        });
        const states = {};
        const missingRows = [];
        ids.forEach((id) => {
            if (Object.prototype.hasOwnProperty.call(byId, id)) {
                states[id] = factState(byId[id]);
            } else {
                states[id] = FACT_STATES.UNKNOWN;
                missingRows.push(id);
            }
        });
        return { ok: true, supplied: !!list, ids: ids, states: states, rows: byId, missingRows: missingRows };
    }

    /** The facts for a document nobody has probed yet. */
    function noFacts(document_) {
        return assetFacts(document_, null);
    }

    // ── The metadata view (documents + records, no bytes) ─────────
    /**
     * Everything the metadata side of a document says about its assets:
     * the references (in document order), the distinct ids, the record
     * each referenced id has (or null), the records that could not be
     * read at all, the slots that point at no asset, and the records
     * nothing points at.
     *
     * Read-only: the document is inspected, never repaired. Repairing
     * here would be a second normalization authority, and a validator
     * that edits what it validates cannot report what it found.
     */
    function assetView(document_) {
        const refs = refsOf(document_);
        const ids = referencedAssetIds(refs);
        const map = (document_ && document_.assets && typeof document_.assets === 'object') ? document_.assets : {};
        const records = {};
        ids.forEach((id) => {
            const raw = Object.prototype.hasOwnProperty.call(map, id) ? map[id] : undefined;
            records[id] = raw === undefined ? null : normalizeRecord(raw);
        });
        const unreadable = assetIssues(document_);
        // A record that cannot be read is reported ONCE, as unreadable — it is
        // not also "unused", because that second message would describe the
        // same entry as a second problem.
        const broken = {};
        unreadable.forEach((entry) => { broken[entry.assetId] = true; });
        const orphans = Object.keys(map).sort()
            .filter((id) => ids.indexOf(id) === -1 && !broken[id]);
        return {
            ok: true,
            refs: refs,
            ids: ids,
            records: records,
            unreadable: unreadable,
            unlinked: refs.filter((ref) => !ref.assetId),
            orphans: orphans,
        };
    }

    /**
     * The byte arithmetic behind the count/size rules: how many assets
     * are referenced, how big they are together, and which ids have no
     * usable byte count (so the caller can say "not measured" instead of
     * reporting a total that silently skipped a file).
     */
    function assetBytes(view) {
        const records = (view && view.records) || {};
        const ids = (view && Array.isArray(view.ids)) ? view.ids : [];
        let total = 0;
        const unknown = [];      // a record exists, but it carries no byte count
        const missing = [];      // no record at all — a different problem, already reported
        ids.forEach((id) => {
            const record = records[id];
            if (!record) { missing.push(id); return; }
            const size = typeof record.bytes === 'number' && record.bytes >= 0 ? record.bytes : null;
            if (size === null) unknown.push(id);
            else total += size;
        });
        return {
            count: ids.length,
            total: total,
            unknown: unknown,
            missing: missing,
            complete: unknown.length === 0 && missing.length === 0,
        };
    }

    // ── Retention / keep-set (pure; NOTHING is deleted here) ──────
    /**
     * THE RETENTION KEEP-SET (phase 2 decision 4, as amended by 7c).
     *
     * Given every document that could still reach an asset — the
     * current one, the saved snapshot, each undo/redo entry — plus the
     * ids this session created, it answers which asset ids must
     * survive, and which records the target document carries that
     * nothing points at any more.
     *
     * WHAT THIS DOES NOT DO, ON PURPOSE
     *   • It never deletes BYTES. Removing bytes safely needs a
     *     cross-draft ownership proof (the 'assets' object store is
     *     shared by every draft in this database, and 7c has no way to
     *     enumerate what the other drafts reference), so byte removal
     *     stays the caller-invoked primitive it always was.
     *   • It never guesses. With no documents at all it reports
     *     `ok:false, reason:'no-documents'` and an empty keep-set:
     *     "I cannot prove what is unreferenced" must never be read as
     *     "throw it away". A caller that treats a failed analysis as an
     *     empty one is the bug this shape exists to prevent.
     */
    function retention(documents, opts) {
        opts = opts || {};
        const docs = (Array.isArray(documents) ? documents : [])
            .filter((doc) => !!doc && typeof doc === 'object');
        if (!docs.length) {
            return { ok: false, reason: 'no-documents', keep: [], refs: [], orphans: [], plan: null };
        }
        const refs = [];
        docs.forEach((doc) => { refsOf(doc).forEach((ref) => refs.push(ref)); });
        const keep = referencedAssetIds(refs);
        (Array.isArray(opts.sessionIds) ? opts.sessionIds : []).forEach((id) => {
            if (id && keep.indexOf(String(id)) === -1) keep.push(String(id));
        });
        keep.sort();
        const target = (opts.records && typeof opts.records === 'object') ? opts.records : null;
        const orphans = target
            ? Object.keys(target).sort().filter((id) => keep.indexOf(id) === -1)
            : [];
        // The plan is pruneOrphans() and nothing else, so there is ONE
        // refcount rule in this codebase: this function only decides what
        // to hand it. Nothing in this build applies the plan.
        const plan = target ? pruneOrphans(target, keep) : null;
        return { ok: true, reason: null, keep: keep, refs: refs, orphans: orphans, plan: plan };
    }

    NERO.embed.assets = Object.freeze({
        // identity
        sha256Hex: sha256Hex,
        assetIdFromSha: assetIdFromSha,
        identify: identify,
        // sniffing
        sniffMime: sniffMime,
        mimeLabel: mimeLabel,
        describeBytes: describeBytes,
        describeSize: describeSize,
        // names
        sanitiseFilename: sanitiseFilename,
        filenameExtension: filenameExtension,
        uniquifyFilenames: uniquifyFilenames,
        // limits (all numbers come from the argument)
        attachmentLimits: attachmentLimits,
        checkLimits: checkLimits,
        checkFileSize: checkFileSize,
        // records + document queries
        buildRecord: buildRecord,
        normalizeRecord: normalizeRecord,
        assetsOf: assetsOf,
        assetIssues: assetIssues,
        refsOf: refsOf,
        referencedAssetIds: referencedAssetIds,
        documentAssetIds: documentAssetIds,
        pruneOrphans: pruneOrphans,
        // availability facts + the metadata view (7c: what the page probes,
        // what the validation rules read — never a second document model)
        FACT_STATES: FACT_STATES,
        factState: factState,
        assetFacts: assetFacts,
        noFacts: noFacts,
        assetView: assetView,
        assetBytes: assetBytes,
        // retention / keep-set analysis (pure; nothing here deletes bytes)
        retention: retention,
        // constants (read-only; the tables a later step or the schema
        // contract test may inspect)
        LIMIT_KEYS: LIMIT_KEYS,
        RECORD_KEYS: RECORD_KEYS,
        ALLOWED_EXTENSIONS: ALLOWED_EXTENSIONS,
        MIME_BY_EXTENSION: MIME_BY_EXTENSION,
        MAX_FILENAME: MAX_FILENAME,
    });
})(window.NERO);
