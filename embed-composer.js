// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed-composer.js
//
// The single-embed editor + Discord message preview renderer,
// EXTRACTED from manage/embedbuilder.html so the Minigames v2
// builder (systems/minigame_builder.html) and the old Embed
// Builder page share ONE implementation. The old page is the
// reference: its per-embed card markup, its preview chrome, and
// its payload/normalization shapes are ported verbatim here; the
// only additions are the optional `components` rows (the engine's
// JSON, rendered as Discord action rows so the preview can never
// diverge from the real game message), the pluggable lookups
// the mention renderer needs, and the optional
// `attachmentPreviewUrl` hook that lets a page own its Blob-URL
// lifecycle so previews never leak object URLs (see below).
//
// Consumed by:
//   * manage/embedbuilder.html   — multi-embed composer (unchanged
//                                   behavior; its inline editor /
//                                   preview / payload code now
//                                   delegates to this module)
//   * systems/minigame_builder.html — one embed + live component
//                                   rows per game type
//
// No server round-trips, no storage: pure view + payload helpers.
// Styles live in static/css/embed-composer.css (also extracted,
// verbatim, from the old page).
// ═══════════════════════════════════════════════════════════════
window.EmbedComposer = (function () {
    'use strict';

    // ── Escaping ──────────────────────────────────────────────────
    // String-based (no DOM allocation per call) — equivalent output
    // to the old page's textContent-div helper for all five HTML
    // specials.
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }
    function attr(s) { return esc(s); }

    function blankEmbed() {
        return { title: '', description: '', color: '#7c5cbf', author: '',
                 footer: '', thumbnail: '', image: '', fields: [] };
    }
    function embedHasContent(e) {
        return !!(e.title || e.description || e.author || e.footer ||
                  e.image || e.thumbnail || (e.fields && e.fields.length));
    }

    // ── Attachment preview URLs ───────────────────────────────────
    // The old inline renderer called URL.createObjectURL() fresh on EVERY
    // render (every keystroke, every embed edit) and never revoked anything
    // — an unbounded per-keystroke leak of blob: URLs. Once enough piled
    // up, createObjectURL() could start throwing and abort the render
    // mid-loop, blanking the list/preview while `attachments` (what Send
    // actually reads) stayed intact — i.e. Discord could receive a file
    // the UI wasn't showing.
    //
    // So: mint at most ONE object URL per Blob and reuse it.
    //   * Pages that OWN their attachment lifecycle (Embed Builder — it
    //     caches on the attachment object, revokes on remove/send/clear)
    //     pass `data.attachmentPreviewUrl` and are fully in control.
    //   * Pages that don't (minigame builder) fall back to this WeakMap
    //     cache, which is at least bounded by the number of Blobs created
    //     in the session instead of by the number of renders. Entries are
    //     dropped with the Blob itself; we deliberately never revoke those
    //     (revoking something a still-mounted <img> points at is worse than
    //     leaking until unload).
    const _blobUrlCache = (typeof WeakMap === 'function') ? new WeakMap() : null;

    function resolveAttachmentPreviewUrl(a, resolver) {
        if (!a) return null;
        if (typeof resolver === 'function') return resolver(a);
        if (a.source === 'remote') return a.url || null; // existing Discord attachment
        if (a.url) return a.url;
        if (!a.blob || typeof URL === 'undefined' || !URL.createObjectURL) return null;
        if (_blobUrlCache) {
            const hit = _blobUrlCache.get(a.blob);
            if (hit) return hit;
        }
        let url = null;
        try {
            url = URL.createObjectURL(a.blob);
        } catch (e) {
            console.error('[embed-composer] createObjectURL failed for', a.name, e);
            return null;
        }
        if (_blobUrlCache) {
            try { _blobUrlCache.set(a.blob, url); } catch (e) { /* non-extensible blob */ }
        }
        return url;
    }

    // ── Graceful degradation: can we actually DISPLAY a blob: URL? ──────
    // A blob: src is only half the story. A Content-Security-Policy
    // `img-src` that omits `blob:` lets the attribute set, the object URL
    // stay alive, and the <img> render as an empty box — no network
    // request, no failed request, just a console line the admin never sees.
    // That is exactly how "attachments don't appear" gets reported as a
    // broken upload feature when nothing about the upload ever failed.
    //
    // So we PROBE instead of assuming: load a 1×1 PNG from a throwaway
    // blob URL into a detached <img> and watch which event fires. The
    // result is cached for the whole document (probing per attachment
    // would cost a decode each time), and a blocked CSP flips us to
    // data: URIs, which are CSP-exempt under any sane img-src.
    //
    // The probe image below is a real, complete 1×1 transparent PNG.
    const _PNG_1x1 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ'
        'AAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==';
    let _blobOk = null;      // null = not probed yet
    let _blobProbe = null;   // in-flight promise

    // Test/debug escape hatch: forget the cached verdict so a caller can
    // re-probe (used by the harness to exercise both CSP outcomes).
    function _resetBlobProbe() { _blobOk = null; _blobProbe = null; }

    // Files small enough to inline as a data: URI. 25MB of image would be a
    // ~33MB base64 string per attachment spliced into innerHTML — on a weak
    // laptop that is a freeze, not a preview. Beyond this we show a clear
    // "no preview" tile and say why, which beats a silently blank box.
    const DATA_URL_MAX_BYTES = 5 * 1024 * 1024;

    // Human-readable copy for every way a preview can legitimately be
    // unavailable. A blank tile is never an acceptable answer — the whole
    // point is that the admin can tell "no preview" from "broken feature".
    const NO_PREVIEW_HINT = {
        'too-large-for-preview': 'File is larger than the inline-preview limit — it will still be sent.',
        'no-blob': 'No file data in this browser tab — re-add the file.',
        'decode-failed': 'This browser could not read the file for previewing.',
        'remote-url-missing': 'Attachment reference has no URL.',
    };

    function atobPolyfill(s) {
        if (typeof atob === 'function') return atob(s);
        if (typeof Buffer !== 'undefined') return Buffer.from(s, 'base64').toString('binary');
        throw new Error('no base64 decoder available');
    }
    function base64ToBlob(b64, type) {
        const bin = atobPolyfill(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i) & 0xff;
        return new Blob([bytes], { type: type || 'image/png' });
    }

    function canRenderBlobUrls() {
        if (_blobOk !== null) return Promise.resolve(_blobOk);
        if (_blobProbe) return _blobProbe;
        _blobProbe = new Promise((resolve) => {
            let settled = false;
            const done = (ok) => {
                if (settled) return;
                settled = true;
                _blobOk = ok;
                if (!ok) {
                    console.warn('[embed-composer] blob: URLs are not renderable in this '
                        + 'document (blocked by CSP img-src, or createObjectURL unavailable). '
                        + 'Local attachment previews fall back to data: URIs — previews may '
                        + 'use more memory and are skipped above '
                        + (DATA_URL_MAX_BYTES / 1024 / 1024).toFixed(0) + 'MB.');
                }
                try { if (url) URL.revokeObjectURL(url); } catch (e) { /* fine */ }
                _blobProbe = null;
                resolve(ok);
            };
            let url = null;
            try {
                if (typeof URL === 'undefined' || !URL.createObjectURL || typeof Blob !== 'function'
                    || typeof Image !== 'function' || typeof document === 'undefined') return done(false);
                url = URL.createObjectURL(base64ToBlob(_PNG_1x1, 'image/png'));
                const img = new Image();
                img.onload = () => done(true);
                img.onerror = () => done(false);
                img.src = url;
                // CSP violations surface as `error` in every current browser,
                // but never depend on it: a blob that hasn't decoded inside
                // this window is treated as unusable.
                setTimeout(() => done(false), 1500);
            } catch (e) {
                done(false);
            }
        });
        return _blobProbe;
    }

    function blobToDataUrl(blob) {
        if (typeof FileReader === 'function' && blob) {
            return new Promise((resolve, reject) => {
                const fr = new FileReader();
                fr.onload = () => resolve(fr.result);
                fr.onerror = () => reject(fr.error || new Error('FileReader failed'));
                fr.readAsDataURL(blob);
            });
        }
        // No FileReader (old WebKit, exotic embedders): reject and let the
        // caller degrade to a "no preview" tile rather than throw.
        return Promise.reject(new Error('no FileReader in this environment'));
    }

    /**
     * The ONE place that decides how a local attachment gets previewed.
     * Order: cached → remote → blob: (if the document can render it) →
     * data: (small files only). Never throws; `null` means "no preview,
     * and here is why" — callers must show that reason, not a blank box.
     *
     * @param {object} a attachment record { name, type, size, blob, source?, url? }
     * @param {object} [opts] { allowBlob?: boolean }
     * @returns {Promise<{url: string|null, kind: string, reason: string}>}
     */
    function previewUrlFor(a, opts) {
        opts = opts || {};
        if (!a) return Promise.resolve({ url: null, kind: 'none', reason: 'no-attachment' });
        if (a._previewUrl) return Promise.resolve({ url: a._previewUrl, kind: 'blob', reason: '' });
        if (a._dataUrl) return Promise.resolve({ url: a._dataUrl, kind: 'data', reason: '' });
        if (a.source === 'remote') {
            return Promise.resolve(a.url
                ? { url: a.url, kind: 'remote', reason: '' }
                : { url: null, kind: 'none', reason: 'remote-url-missing' });
        }
        if (a.url) return Promise.resolve({ url: a.url, kind: 'remote', reason: '' });
        if (!a.blob) return Promise.resolve({ url: null, kind: 'none', reason: 'no-blob' });
        // One resolution per attachment. Without this guard, two renders that
        // land before the probe settles would each mint an object URL and one
        // of them would leak (and the loser's _previewUrl would be overwritten,
        // leaving an orphan URL the revoke step can no longer see).
        if (a._previewPromise) return a._previewPromise;
        const allowBlob = opts.allowBlob !== false;
        const run = Promise.resolve(allowBlob ? canRenderBlobUrls() : false).then((blobOk) => {
            if (blobOk) {
                try {
                    const url = URL.createObjectURL(a.blob);
                    a._previewUrl = url;
                    return { url, kind: 'blob', reason: '' };
                } catch (e) {
                    console.error('[embed-composer] createObjectURL failed for', a.name, e);
                    // fall through to data: — a throw here must not mean "no preview"
                }
            }
            if (a.size > DATA_URL_MAX_BYTES) {
                return { url: null, kind: 'none', reason: 'too-large-for-preview' };
            }
            return blobToDataUrl(a.blob).then(
                (url) => { a._dataUrl = url; return { url, kind: 'data', reason: '' }; },
                (e) => {
                    console.error('[embed-composer] data: preview failed for', a.name, e);
                    return { url: null, kind: 'none', reason: 'decode-failed' };
                });
        });
        a._previewPromise = run;
        // The promise is only a de-dupe key — drop it once settled so a
        // later retry (the "no preview → retry" affordance) isn't short-
        // circuited forever by a resolved promise.
        run.then(() => { a._previewPromise = null; },
                () => { a._previewPromise = null; });
        return run;
    }

    // ── Textarea insertion helpers ────────────────────────────────
    // (verbatim from the old page — the emoji/mention tooling on
    // both pages inserts through these.)
    function insertAtCursor(textarea, textToInsert, opts) {
        opts = opts || {};
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        const before = textarea.value.slice(0, start);
        const after = textarea.value.slice(end);
        let newValue = before + textToInsert + after;
        let cursorPos = start + textToInsert.length;

        if (opts.cursorOffset !== undefined) {
            cursorPos = start + opts.cursorOffset;
        }

        const maxLen = parseInt(textarea.getAttribute('maxlength')) || Infinity;
        if (newValue.length > maxLen) {
            const overflow = newValue.length - maxLen;
            // Trim from the inserted text itself, not the user's existing content.
            newValue = before + textToInsert.slice(0, Math.max(0, textToInsert.length - overflow)) + after;
        }

        textarea.value = newValue;
        textarea.focus();
        textarea.setSelectionRange(cursorPos, opts.selectInserted ? start + textToInsert.length : cursorPos);
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function wrapSelection(textarea, wrapper) {
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        const selected = textarea.value.slice(start, end);
        if (selected.length) {
            const before = textarea.value.slice(0, start);
            const after = textarea.value.slice(end);
            textarea.value = before + wrapper + selected + wrapper + after;
            textarea.focus();
            textarea.setSelectionRange(start + wrapper.length, start + wrapper.length + selected.length);
        } else {
            insertAtCursor(textarea, wrapper + wrapper, { cursorOffset: wrapper.length });
            return;
        }
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
    }

    // ═══════════════════════════════════════════════════════════════
    // PER-EMBED EDITOR (accordion card, verbatim markup from the
    // old page's renderEmbeds/wireEmbedEvents)
    //
    // opts:
    //   listEl      — container that holds the accordion
    //   counterEl   — "n / max" counter (optional)
    //   maxEmbeds   — cap (button toasts are the PAGE's job)
    //   getEmbeds   — () => array of embed objects (mutated in place)
    //   getActive   — () => active index (-1 = none expanded)
    //   setActive   — (i) => set active index
    //   onChange    — () => called after ANY field mutation (preview/
    //                 draft/history hooks belong to the page)
    //   onDuplicate — (i) => page-level duplicate (max check, toast)
    //   onDelete    — (i) => page-level delete
    //   hideActions — true = the per-card ⧉/🗑 buttons are not
    //                 rendered (single-embed pages like the minigames
    //                 builder — the game has exactly one embed)
    //
    // Returns { render() } — call render() after structural changes
    // (add/delete/load/clear). Field inputs mutate the embed objects
    // in place and fire onChange() WITHOUT re-rendering the DOM (the
    // user's caret stays put, exactly as on the old page).
    // ═══════════════════════════════════════════════════════════════
    function mountEditor(opts) {
        const listEl = opts.listEl;

        function render() {
            const embeds = opts.getEmbeds();
            const active = opts.getActive();
            if (opts.counterEl) {
                opts.counterEl.textContent =
                    `${embeds.length} / ${opts.maxEmbeds}`;
            }
            listEl.innerHTML = embeds.map((e, i) => `
        <div class="eb-embed-card ${i === active ? 'eb-expanded' : ''}" data-idx="${i}">
            <div class="eb-embed-head" data-toggle="${i}">
                <span class="eb-embed-chevron">›</span>
                <span class="eb-embed-title-preview">Embed ${i + 1}${e.title ? ' — ' + esc(e.title) : ''}</span>
                ${opts.hideActions ? '' : `
                <div class="eb-embed-actions">
                    <button type="button" class="btn btn-sm btn-secondary" data-dup="${i}" title="Duplicate">⧉</button>
                    <button type="button" class="btn btn-sm btn-danger" data-del="${i}" title="Delete">🗑️</button>
                </div>`}
            </div>
            <div class="eb-embed-body">
                <div class="form-row">
                    <div class="form-group">
                        <label class="form-label">Title</label>
                        <input class="form-input" data-field="title" data-idx="${i}" value="${attr(e.title)}">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Color</label>
                        <div style="display:flex;gap:8px;align-items:center;">
                            <input type="color" data-field="color" data-idx="${i}" value="${attr(e.color || '#7c5cbf')}"
                                style="width:42px;height:36px;border-radius:8px;border:1px solid var(--border);background:transparent;cursor:pointer;padding:2px;">
                            <input class="form-input" data-field="color-hex" data-idx="${i}" value="${attr(e.color || '#7c5cbf')}" style="flex:1;">
                        </div>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Author</label>
                        <input class="form-input" data-field="author" data-idx="${i}" value="${attr(e.author)}">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Footer</label>
                        <input class="form-input" data-field="footer" data-idx="${i}" value="${attr(e.footer)}">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Thumbnail URL</label>
                        <input class="form-input" data-field="thumbnail" data-idx="${i}" value="${attr(e.thumbnail)}" placeholder="https://...">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Image URL</label>
                        <input class="form-input" data-field="image" data-idx="${i}" value="${attr(e.image)}" placeholder="https://...">
                    </div>
                    <div class="form-group" style="grid-column:1/-1;">
                        <label class="form-label">Description <span class="text-muted text-sm">(${(e.description || '').length} / 4096)</span></label>
                        <textarea class="form-input" data-field="description" data-idx="${i}" rows="3" maxlength="4096">${esc(e.description)}</textarea>
                    </div>
                </div>
                <div class="form-group">
                    <label class="form-label">Fields</label>
                    <div data-fields-for="${i}">
                        ${(e.fields || []).map((f, fi) => `
                        <div class="eb-embed-fieldrow" data-field-idx="${fi}">
                            <input class="form-input" placeholder="Name" data-fieldname="${i}:${fi}" value="${attr(f.name)}">
                            <input class="form-input" placeholder="Value" data-fieldvalue="${i}:${fi}" value="${attr(f.value)}">
                            <label class="form-check" style="white-space:nowrap;"><input type="checkbox" data-fieldinline="${i}:${fi}" ${f.inline ? 'checked' : ''}> Inline</label>
                            <button type="button" class="btn btn-sm btn-danger" data-fielddel="${i}:${fi}">✕</button>
                        </div>`).join('')}
                    </div>
                    <button type="button" class="btn btn-sm btn-secondary" data-addfield="${i}" style="margin-top:6px;">+ Add Field</button>
                </div>
            </div>
        </div>
    `).join('');
            wire();
        }

        function wire() {
            listEl.querySelectorAll('[data-toggle]').forEach(head => {
                head.addEventListener('click', () => {
                    const i = parseInt(head.dataset.toggle);
                    opts.setActive(opts.getActive() === i ? -1 : i);
                    render();
                });
            });
            listEl.querySelectorAll('[data-dup]').forEach(btn =>
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    opts.onDuplicate && opts.onDuplicate(parseInt(btn.dataset.dup));
                }));
            listEl.querySelectorAll('[data-del]').forEach(btn =>
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    opts.onDelete && opts.onDelete(parseInt(btn.dataset.del));
                }));

            listEl.querySelectorAll('[data-field]').forEach(el => {
                el.addEventListener('input', () => {
                    const embeds = opts.getEmbeds();
                    const i = parseInt(el.dataset.idx);
                    const field = el.dataset.field;
                    if (field === 'color') {
                        embeds[i].color = el.value;
                        const hexInput = listEl.querySelector(
                            `[data-field="color-hex"][data-idx="${i}"]`);
                        if (hexInput) hexInput.value = el.value;
                    } else if (field === 'color-hex') {
                        if (/^#[0-9A-Fa-f]{6}$/.test(el.value)) {
                            embeds[i].color = el.value;
                            const picker = listEl.querySelector(
                                `[data-field="color"][data-idx="${i}"]`);
                            if (picker) picker.value = el.value;
                        }
                    } else {
                        embeds[i][field] = el.value;
                    }
                    if (field === 'title') {
                        const label = listEl.querySelector(
                            `.eb-embed-card[data-idx="${i}"] .eb-embed-title-preview`);
                        if (label) label.textContent =
                            `Embed ${i + 1}${el.value ? ' — ' + el.value : ''}`;
                    }
                    if (field === 'description') {
                        const counter = el.closest('.form-group').querySelector('.text-muted');
                        if (counter) counter.textContent = `(${el.value.length} / 4096)`;
                    }
                    opts.onChange && opts.onChange();
                });
            });

            listEl.querySelectorAll('[data-addfield]').forEach(btn => {
                btn.addEventListener('click', () => {
                    const embeds = opts.getEmbeds();
                    const i = parseInt(btn.dataset.addfield);
                    embeds[i].fields = embeds[i].fields || [];
                    if (embeds[i].fields.length >= 25) {
                        window.showToast && showToast('Discord caps embeds at 25 fields', 'warning');
                        return;
                    }
                    embeds[i].fields.push({ name: '', value: '', inline: false });
                    render();
                    opts.onChange && opts.onChange();
                });
            });
            listEl.querySelectorAll('[data-fieldname]').forEach(el =>
                el.addEventListener('input', () => {
                    const [i, fi] = el.dataset.fieldname.split(':').map(Number);
                    opts.getEmbeds()[i].fields[fi].name = el.value;
                    opts.onChange && opts.onChange();
                }));
            listEl.querySelectorAll('[data-fieldvalue]').forEach(el =>
                el.addEventListener('input', () => {
                    const [i, fi] = el.dataset.fieldvalue.split(':').map(Number);
                    opts.getEmbeds()[i].fields[fi].value = el.value;
                    opts.onChange && opts.onChange();
                }));
            listEl.querySelectorAll('[data-fieldinline]').forEach(el =>
                el.addEventListener('change', () => {
                    const [i, fi] = el.dataset.fieldinline.split(':').map(Number);
                    opts.getEmbeds()[i].fields[fi].inline = el.checked;
                    opts.onChange && opts.onChange();
                }));
            listEl.querySelectorAll('[data-fielddel]').forEach(el =>
                el.addEventListener('click', () => {
                    const [i, fi] = el.dataset.fielddel.split(':').map(Number);
                    opts.getEmbeds()[i].fields.splice(fi, 1);
                    render();
                    opts.onChange && opts.onChange();
                }));
        }

        return { render };
    }

    // ═══════════════════════════════════════════════════════════════
    // DISCORD MESSAGE PREVIEW
    //
    // data:
    //   content      — message text (markdown)
    //   embeds       — [embed objects] (see blankEmbed)
    //   attachments  — optional [ { name, type, blob } ] (old page only)
    //   components   — optional: the ENGINE's component rows —
    //                  [[{label, style, disabled, emoji?}, ...], ...]
    //                  (utils/minigame_engine.initial_component_rows).
    //                  Rendered as Discord action rows under the
    //                  embeds — this is what keeps the preview
    //                  identical to the real game message.
    //   botIdentity  — { name, avatar }
    //   lookups      — { roles: {id:{name,color}}, channels: {id:{name}},
    //                  users: {id:name}, onUserResolve: fn|null }
    //                  (all optional — unresolved mentions render as
    //                  raw IDs, exactly like Discord does)
    //   emptyText    — placeholder when nothing is set
    // ═══════════════════════════════════════════════════════════════
    const EMOJI_UNICODE_RE = /\p{Extended_Pictographic}(\u200d\p{Extended_Pictographic})*\ufe0f?/gu;
    const TOKEN_RE = /<(a?):(\w+):(\d+)>|<#(\d+)>|<@&(\d+)>|<@!?(\d+)>/g;

    function emojiUrl(id, animated) {
        return `https://cdn.discordapp.com/emojis/${id}.${animated ? 'gif' : 'png'}`;
    }

    function renderToken(match, lookups) {
        let m;
        TOKEN_RE.lastIndex = 0;
        m = TOKEN_RE.exec(match);
        if (!m) return esc(match);
        const [, animFlag, ename, eid, chid, rid, uid] = m;
        lookups = lookups || {};
        if (eid) {
            return `<img class="eb-inline-emoji" src="${emojiUrl(eid, !!animFlag)}" alt=":${attr(ename)}:`;
        }
        if (chid) {
            const ch = (lookups.channels || {})[chid];
            return `<span class="eb-mention">#${esc(ch ? ch.name : chid)}</span>`;
        }
        if (rid) {
            const role = (lookups.roles || {})[rid];
            const label = role ? role.name : rid;
            let style = '';
            if (role && role.color) {
                style = ` style="background:${role.color}33;color:${role.color};"`;
            }
            return `<span class="eb-mention"${style}>@${esc(label)}</span>`;
        }
        if (uid) {
            const users = lookups.users || {};
            if (users[uid]) return `<span class="eb-mention">@${esc(users[uid])}</span>`;
            if (typeof lookups.onUserResolve === 'function') lookups.onUserResolve(uid);
            return `<span class="eb-mention">@${esc(uid)}</span>`;
        }
        return esc(match);
    }

    // Discord message-content markdown + mention/emoji rendering —
    // matches what the real client does closely enough for an
    // accurate preview (verbatim logic from the old page, lookups
    // now passed in instead of read from module globals).
    function renderDiscordMarkup(text, opts) {
        opts = opts || {};
        if (!text) return { html: '', isEmojiOnly: false };

        const lookups = opts.lookups || {};
        const tokens = [];
        let working = text.replace(TOKEN_RE, (match) => {
            tokens.push(match);
            return `\u0001${tokens.length - 1}\u0002`;
        });

        // Emoji-only-line sizing: Discord renders a message as large emoji
        // when, once every recognized token/emoji is stripped out, nothing
        // but whitespace is left.
        let isEmojiOnly = false;
        if (opts.checkEmojiOnly) {
            const stripped = working.replace(/\u0001\d+\u0002/g, '').replace(EMOJI_UNICODE_RE, '').trim();
            isEmojiOnly = stripped.length === 0 && (tokens.length + (text.match(EMOJI_UNICODE_RE) || []).length) > 0;
        }

        let escaped = esc(working);

        // Markdown — bold before italic so `**x**` isn't half-consumed by
        // the single-asterisk italic pattern first.
        escaped = escaped.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
        escaped = escaped.replace(/__([\s\S]+?)__/g, '<u>$1</u>');
        escaped = escaped.replace(/\*([\s\S]+?)\*/g, '<em>$1</em>');
        escaped = escaped.replace(/~~([\s\S]+?)~~/g, '<s>$1</s>');

        // Standalone unicode emoji get the large-size treatment too.
        if (isEmojiOnly) {
            escaped = escaped.replace(EMOJI_UNICODE_RE, (m) => `<span>${m}</span>`);
        }

        escaped = escaped.replace(/\u0001(\d+)\u0002/g, (m, idx) => renderToken(tokens[parseInt(idx)], lookups));

        return { html: escaped, isEmojiOnly };
    }

    function fmtPreviewTime() {
        const d = new Date();
        let h = d.getHours();
        const ampm = h >= 12 ? 'PM' : 'AM';
        h = h % 12 || 12;
        const m = String(d.getMinutes()).padStart(2, '0');
        return `Today at ${h}:${m} ${ampm}`;
    }

    // ── Engine component rows → Discord action-row HTML ───────────
    // rows: [[btn, btn, ...], ...]; btn: {label, style, disabled,
    // emoji?} as produced by utils.minigame_engine (style: 1 primary,
    // 2 secondary, 3 success, 4 danger). Emojis may be unicode,
    // <:name:id> tokens, or url-only strings.
    const COMP_STYLE_CLASS = { 1: 'eb-cbtn-primary', 2: 'eb-cbtn-secondary',
                               3: 'eb-cbtn-success', 4: 'eb-cbtn-danger' };

    function emojiDisplay(emoji) {
        if (!emoji) return '';
        const t = String(emoji);
        // Full tokens only: <a:name:id> / <name:id> — anything else
        // (unicode chars, plain text) renders as-is.
        const m = t.match(/^<(a?):(\w+):(\d+)>$/);
        if (m) {
            return `<img class="eb-inline-emoji" src="${emojiUrl(m[3], m[1] === 'a')}" alt=":${m[2]}:`;
        }
        return esc(t);
    }

    function componentRowsHtml(rows) {
        if (!rows || !rows.length) return '';
        return rows.map(row => `
            <div class="eb-comp-row">
                ${(row || []).map(b => {
                    const cls = COMP_STYLE_CLASS[b.style] || 'eb-cbtn-secondary';
                    const dis = b.disabled ? ' disabled' : '';
                    const em = b.emoji ? ` <span class="eb-cbtn-emoji">${emojiDisplay(b.emoji)}</span>` : '';
                    return `<button type="button" class="eb-comp-btn ${cls}${dis}" tabindex="-1">${esc(b.label)}${em}</button>`;
                }).join('')}
            </div>`).join('');
    }

    function renderPreview(box, data) {
        data = data || {};
        const lookups = data.lookups || {};
        const bot = data.botIdentity || { name: 'Bot', avatar: null };
        const embeds = data.embeds || [];
        const attachments = data.attachments || [];
        const components = data.components || [];

        const hasAnything = data.content || attachments.length ||
            embeds.some(embedHasContent) || components.length;

        if (!hasAnything) {
            box.innerHTML = `<div class="eb-preview-empty">${esc(data.emptyText || 'Start typing to see a preview...')}</div>`;
            return;
        }

        let html = `<div class="eb-msg">`;
        html += bot.avatar
            ? `<img class="eb-msg-avatar" src="${attr(bot.avatar)}" alt="">`
            : `<div class="eb-msg-avatar"></div>`;
        html += `<div class="eb-msg-body">`;
        html += `<div class="eb-msg-header">
        <span class="eb-msg-name">${esc(bot.name)}</span>
        <span class="eb-msg-app-badge">APP</span>
        <span class="eb-msg-time">${fmtPreviewTime()}</span>
    </div>`;

        if (data.content) {
            const rendered = renderDiscordMarkup(data.content, { checkEmojiOnly: true, lookups });
            html += `<div class="eb-msg-content${rendered.isEmojiOnly ? ' eb-emoji-only-content' : ''}">${rendered.html}</div>`;
        }

        if (attachments.length) {
            html += `<div class="eb-preview-attachments">`;
            // Images first (in insertion order), then non-images as badges —
            // same split the Embed Builder's own list does.
            const imgHtml = attachments.filter(a => a.type && a.type.startsWith('image/')).map(a => {
                try {
                    // A page that owns its attachment lifecycle (Embed Builder)
                    // hands us a resolver; nobody else should be minting URLs.
                    const url = typeof data.attachmentPreviewUrl === 'function'
                        ? data.attachmentPreviewUrl(a)
                        : resolveAttachmentPreviewUrl(a, null);
                    if (url) return `<img src="${attr(url)}" alt="${attr(a.name)}" loading="lazy" decoding="async">`;
                    // No URL yet is NOT the same as "nothing to show": while an
                    // async preview (data: fallback) is in flight we keep the
                    // slot visible as a pending badge instead of dropping it,
                    // and we surface the real reason once it is known.
                    if (a._previewPending) {
                        return `<span class="badge eb-pe-pending" data-preview-state="pending">⏳ ${esc(a.name)}</span>`;
                    }
                    if (a._previewError) {
                        return `<span class="badge eb-pe-pending" data-preview-state="${attr(a._previewError)}" title="${attr(NO_PREVIEW_HINT[a._previewError] || '')}">🚫 ${esc(a.name)}</span>`;
                    }
                    return `<span class="badge">📄 ${esc(a.name)}</span>`;
                } catch (e) {
                    // Never let one bad attachment abort the whole preview.
                    console.error('[embed-composer] preview failed for attachment', a && a.name, e);
                    return '';
                }
            }).join('');
            html += imgHtml;
            const nonImg = attachments.filter(a => !a.type || !a.type.startsWith('image/'));
            if (nonImg.length) html += nonImg.map(a => `<span class="badge">📄 ${esc(a.name)}</span>`).join('');
            html += `</div>`;
        }

        const nonEmpty = embeds.filter(embedHasContent);
        for (const e of nonEmpty) {
            html += `<div class="eb-preview-embed" style="border-left-color:${attr(e.color || '#7c5cbf')};">`;
            if (e.author) html += `<div class="eb-pe-author">${esc(e.author)}</div>`;
            if (e.title) html += `<div class="eb-pe-title">${esc(e.title)}</div>`;
            if (e.description) html += `<div class="eb-pe-desc">${renderDiscordMarkup(e.description, { lookups }).html}</div>`;
            if (e.fields && e.fields.length) {
                html += `<div class="eb-pe-fields">`;
                html += e.fields.filter(f => f.name || f.value).map(f =>
                    `<div class="eb-pe-field" style="${f.inline ? 'width:31%;' : 'width:100%;'}">
                    <div class="eb-pe-field-name">${esc(f.name)}</div>
                    <div class="eb-pe-field-value">${renderDiscordMarkup(f.value, { lookups }).html}</div>
                </div>`).join('');
                html += `</div>`;
            }
            if (e.image) html += `<img class="eb-pe-image" src="${attr(e.image)}" alt="">`;
            if (e.thumbnail) html += `<img class="eb-pe-thumb" src="${attr(e.thumbnail)}" alt="">`;
            if (e.footer) html += `<div class="eb-pe-footer">${esc(e.footer)}</div>`;
            html += `</div>`;
        }

        // The game's interactive component rows — same JSON the engine
        // posts, so the preview IS the message (embed + components
        // read as one game, not two systems).
        if (components.length) {
            html += `<div class="eb-comp-wrap">${componentRowsHtml(components)}</div>`;
        }

        html += `</div></div>`;
        box.innerHTML = html;
    }

    // ═══════════════════════════════════════════════════════════════
    // PAYLOAD HELPERS (verbatim shapes from the old page)
    // ═══════════════════════════════════════════════════════════════
    function cleanEmbedForPayload(e) {
        const out = {};
        if (e.title) out.title = e.title;
        if (e.description) out.description = e.description;
        if (e.color) { try { out.color = parseInt(e.color.replace('#', ''), 16); } catch (err) {} }
        if (e.author) out.author = { name: e.author };
        if (e.footer) out.footer = { text: e.footer };
        if (e.image) out.image = { url: e.image };
        if (e.thumbnail) out.thumbnail = { url: e.thumbnail };
        if (e.fields && e.fields.length) {
            out.fields = e.fields.filter(f => f.name || f.value).map(f => ({
                name: f.name || '\u200b', value: f.value || '\u200b', inline: !!f.inline,
            }));
        }
        return out;
    }

    function cleanEmbedsForPayload(embeds) {
        return (embeds || []).filter(embedHasContent).map(cleanEmbedForPayload);
    }

    // API-shaped embed ({color:int, author:{name}, footer:{text},
    // image:{url}, ...}) → editor shape. (verbatim logic from the
    // old page's template-load handler)
    function embedFromApi(e) {
        return {
            title: e.title || '',
            description: e.description || '',
            color: e.color !== undefined && e.color !== null && e.color !== ''
                ? (typeof e.color === 'number'
                    ? '#' + e.color.toString(16).padStart(6, '0')
                    : String(e.color))
                : '#7c5cbf',
            author: (e.author && e.author.name) || e.author || '',
            footer: (e.footer && e.footer.text) || e.footer || '',
            thumbnail: (e.thumbnail && e.thumbnail.url) || e.thumbnail || '',
            image: (e.image && e.image.url) || e.image || '',
            fields: (e.fields || []).map(f => ({
                name: f.name || '', value: f.value || '', inline: !!f.inline,
            })),
        };
    }

    function embedsFromApi(list) {
        const out = (list || []).map(embedFromApi);
        return out.length ? out : [blankEmbed()];
    }

    return {
        esc, attr,
        blankEmbed, embedHasContent,
        insertAtCursor, wrapSelection,
        mountEditor,
        renderDiscordMarkup, renderPreview, componentRowsHtml,
        canRenderBlobUrls, previewUrlFor, resolveAttachmentPreviewUrl, _resetBlobProbe,
        NO_PREVIEW_HINT, DATA_URL_MAX_BYTES,
        cleanEmbedForPayload, cleanEmbedsForPayload,
        embedFromApi, embedsFromApi,
        fmtPreviewTime,
    };
})();
