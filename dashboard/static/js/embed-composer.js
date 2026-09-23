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
    //   extraFields — OPTIONAL [{ key, label, placeholder? }] rendered as
    //                 plain text inputs alongside the built-in ones. The
    //                 value is read from `embed[key]` and written back on
    //                 input, exactly like title/author, so a page can add
    //                 fields Discord supports without this module having
    //                 to grow a branch per field. Used by the Embed
    //                 Builder for author icon/url, footer icon, embed url
    //                 and timestamp. Omitting it keeps every existing
    //                 caller byte-identical (minigames builder).
    //
    // Returns { render() } — call render() after structural changes
    // (add/delete/load/clear). Field inputs mutate the embed objects
    // in place and fire onChange() WITHOUT re-rendering the DOM (the
    // user's caret stays put, exactly as on the old page).
    // ═══════════════════════════════════════════════════════════════
    function mountEditor(opts) {
        const listEl = opts.listEl;
        const extraFields = opts.extraFields || [];

        function extraFieldsHtml(e, i) {
            if (!extraFields.length) return '';
            return extraFields.map(f => `
                    <div class="form-group">
                        <label class="form-label">${esc(f.label)}</label>
                        <input class="form-input" data-efield="${attr(f.key)}" data-idx="${i}"
                            value="${attr(e[f.key])}"
                            ${f.type ? `type="${attr(f.type)}"` : ''}
                            ${f.placeholder ? `placeholder="${attr(f.placeholder)}"` : ''}>
                    </div>`).join('');
        }

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
                    ${extraFieldsHtml(e, i)}
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
            // Instrumentation seam (additive, opt-in): the page counts how
            // often the WHOLE editor is rebuilt — the measurement behind
            // "typing must not rebuild the editor".
            opts.onRender && opts.onRender();
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

            // opts.extraFields — model keys written straight back (author
            // icon/url, footer icon, embed url, timestamp). Same contract
            // as the built-in fields: mutate in place, no re-render.
            listEl.querySelectorAll('[data-efield]').forEach(el => {
                el.addEventListener('input', () => {
                    const i = parseInt(el.dataset.idx);
                    const embeds = opts.getEmbeds();
                    if (!embeds[i]) return;
                    embeds[i][el.dataset.efield] = el.value;
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

    // ── Code spans and fenced blocks ──────────────────────────────
    // Discord renders code literally (no bold, no mentions, no emoji) and a
    // fenced block's opening line is its LANGUAGE, which the client uses for
    // highlighting and never shows as content.
    //
    // ONE left-to-right scan implements that. A scan cannot mistake its own
    // output for input, which the previous pair of sequential regexes could:
    // the placeholder a fence left behind got picked up as the body of the
    // following inline-code pass, so   `` ```x``` ``   put the placeholder's
    // raw control characters into the preview (string.replace never rescans
    // its own replacement text, so the restore pass could not fix it).
    // Scanning once also gives ``double`` spans, nested runs and unclosed
    // runs the behaviour the client has, because the run length — and
    // nothing else — decides where code ends.
    //
    //   * a run of backticks opens code; it closes at the next run of the
    //     SAME length, except that a fence may close on a longer run
    //     (CommonMark's rule, and the one that makes  `` `x` ``  a code span
    //     holding a backtick instead of two broken halves);
    //   * a run that never closes stays exactly as typed;
    //   * onChunk(body, isFence, atLineStart) decides what the code becomes.
    function scanCode(text, onChunk) {
        const TICK = 96;                        // '`'
        let out = '';
        let i = 0;
        while (i < text.length) {
            if (text.charCodeAt(i) !== TICK) { out += text[i]; i += 1; continue; }
            let run = 1;
            while (text.charCodeAt(i + run) === TICK) run += 1;
            const bodyStart = i + run;
            const isFence = run >= 3;
            let closeAt = -1;
            let closeLen = 0;
            for (let j = bodyStart; j < text.length;) {
                if (text.charCodeAt(j) !== TICK) { j += 1; continue; }
                let n = 1;
                while (text.charCodeAt(j + n) === TICK) n += 1;
                if (isFence ? n >= run : n === run) { closeAt = j; closeLen = n; break; }
                j += n;
            }
            if (closeAt === -1) {               // unclosed: leave the ticks be
                out += text.slice(i, bodyStart);
                i = bodyStart;
                continue;
            }
            const lineStart = text.lastIndexOf('\n', i - 1) + 1;
            const atLineStart = !text.slice(lineStart, i).trim();
            out += onChunk(text.slice(bodyStart, closeAt), isFence, atLineStart);
            i = closeAt + closeLen;
        }
        return out;
    }

    // A fence's opening line is its language, so it is dropped — together
    // with the newline after it and a trailing one, which would otherwise
    // leave the block starting or ending on a blank line. Only for a fence
    // that starts a line: ```x``` typed mid-sentence has no language line
    // and keeps its content (as it did before this pass).
    function fencedCodeBody(body) {
        const firstLine = body.match(/^[^\n]*\n/);
        if (firstLine && /^[ \t]*[A-Za-z0-9_+#.-]*[ \t]*\r?\n$/.test(firstLine[0])) {
            body = body.slice(firstLine[0].length);
        }
        return body.replace(/^\r?\n/, '').replace(/\r?\n[ \t]*$/, '');
    }

    // CommonMark: content that both starts and ends with a space (and is not
    // all spaces) loses one space at each end — that is what makes
    // `` `x` `` read as a code span holding a backtick.
    function inlineCodeBody(body) {
        return (/^[ \t]/.test(body) && /[ \t]$/.test(body) && body.trim())
            ? body.slice(1, -1) : body;
    }

    // C0 controls that can never render. They are also the alphabet the
    // code/token placeholders use, so they are kept out of the source text
    // and scrubbed from the output as a backstop (see renderDiscordMarkup).
    const CONTROL_CHARS_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g;

    // Discord message-content markdown + mention/emoji rendering —
    // matches what the real client does closely enough for an
    // accurate preview (verbatim logic from the old page, lookups
    // now passed in instead of read from module globals).
    function renderDiscordMarkup(text, opts) {
        opts = opts || {};
        if (!text) return { html: '', isEmojiOnly: false };

        // A pasted control character is deleted before anything else: it
        // cannot render (a browser shows a box or nothing), and one shaped
        // like \u0003…\u0004 would otherwise masquerade as a placeholder of
        // this function's own making.
        text = String(text).replace(CONTROL_CHARS_RE, '');
        if (!text) return { html: '', isEmojiOnly: false };

        const lookups = opts.lookups || {};
        const tokens = [];
        let working = text.replace(TOKEN_RE, (match) => {
            tokens.push(match);
            return `\u0001${String(tokens.length - 1).padStart(4, '0')}\u0002`;
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

        // ── Code spans and code blocks first, and OUT of the way ──────
        // Discord does not interpret markdown inside `code` or ```blocks```
        // — but this renderer used to: a value like
        //     `**not bold**`
        // came out bold, i.e. the preview showed one thing and Discord
        // another. Swap them for placeholders before the markdown pass and
        // splice them back afterwards; the content is already escaped at
        // this point, so what goes back in is text, never markup.
        //
        // The stash key is fixed-width so the two kinds of placeholder can
        // never share an index: token 5 is \u0001\u00005\u0002, code chunk 5
        // is \u0003\u00005\u0004. The old form was `\u0003` + index + `\u0004`
        // — one character shorter — which is exactly why `` ```x``` ``
        // matched the INLINE pattern, re-stashing its own placeholder and
        // leaving \u0003\u00040\u0004 in the output.
        const codeChunks = [];
        const stashCode = (html) => {
            codeChunks.push(html);
            return `\u0003${String(codeChunks.length - 1).padStart(4, '0')}\u0004`;
        };
        escaped = scanCode(escaped, (body, isFence, atLineStart) => isFence
            ? stashCode(`<pre class="eb-code-block"><code>${fencedCodeBody(body)}</code></pre>`)
            : stashCode(`<code class="eb-code">${inlineCodeBody(body)}</code>`));

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

        // Code back first... The lookup is a direct index, not a search:
        // every placeholder this pass produced is resolved, and a miss can
        // only mean a placeholder that came from OUTSIDE (see the scrub
        // below) — never silently replaced with empty markup.
        escaped = escaped.replace(/\u0003(\d{4})\u0004/g, (m, idx) => {
            const chunk = codeChunks[parseInt(idx, 10)];
            return chunk === undefined ? m : chunk;
        });
        // ...then mentions/emoji, whose URLs are generated here (never
        // user-supplied) and so must NOT be stashed as code.
        escaped = escaped.replace(/\u0001(\d{4})\u0002/g, (m, idx) => {
            const token = tokens[parseInt(idx, 10)];
            return token === undefined ? m : renderToken(token, lookups);
        });

        // Backstop: nothing of this pass's placeholder alphabet may reach
        // the DOM. A browser renders these as a box or as nothing, so text
        // pasted with one inside would otherwise show up as a mystery glyph.
        escaped = escaped.replace(CONTROL_CHARS_RE, '');

        return { html: escaped, isEmojiOnly };
    }

    // ── Field values Discord accepts as media / links ─────────────
    // Everything that ends up in `src` or `href` goes through these. A
    // preview that accepts `javascript:` in a title link is a preview that
    // can execute whatever was pasted into it, and `<img src>` on a
    // `attachment://` URL is a guaranteed broken-image icon — the preview
    // shows nothing for an unresolved attachment reference instead
    // (Phase 2 resolves it to a real blob/CDN URL through
    // `data.resolveImageSrc`).
    function mediaSrc(url) {
        const u = String(url == null ? '' : url).trim();
        if (!u) return '';
        if (/^(https?:|blob:|data:)/i.test(u)) return u;
        return '';
    }
    function safeHref(url) {
        const u = String(url == null ? '' : url).trim();
        return /^https?:\/\//i.test(u) ? u : '';
    }
    function imgSrc(value, data) {
        const raw = String(value == null ? '' : value).trim();
        if (!raw) return '';
        if (/^attachment:\/\//i.test(raw)) {
            // Seam for the asset layer (Phase 2): a page may hand in a
            // resolver; without one, an unresolved attachment reference
            // renders as "no image" rather than as a broken image.
            const resolved = (data && typeof data.resolveImageSrc === 'function')
                ? data.resolveImageSrc(raw) : null;
            return mediaSrc(resolved);
        }
        return mediaSrc(raw);
    }

    // Discord's own "Today at 6:42 PM" for today's timestamps and a plain
    // local date-time otherwise. Deliberately an approximation with a
    // comment saying so: the real client formats per user locale, and the
    // preview must never claim to be the client down to the last detail.
    function fmtDiscordTimestamp(value) {
        if (!value) return '';
        const d = new Date(value);
        if (isNaN(d.getTime())) return '';
        const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        const now = new Date();
        const sameDay = d.getFullYear() === now.getFullYear()
            && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
        if (sameDay) return `Today at ${time}`;
        return `${d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })} ${time}`;
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

        // Icon sizing is inline (not a new stylesheet rule) so this stays
        // an additive change to the shared composer CSS: 16px rounded
        // avatar for the author, 20px round for the footer, matching what
        // Discord draws (thumbnail for the author, a circle for the footer).
        const AUTHOR_ICON_STYLE = 'width:16px;height:16px;border-radius:3px;object-fit:cover;flex-shrink:0;';
        const FOOTER_ICON_STYLE = 'width:20px;height:20px;border-radius:50%;object-fit:cover;flex-shrink:0;';

        const nonEmpty = embeds.filter(embedHasContent);
        for (const e of nonEmpty) {
            html += `<div class="eb-preview-embed" style="border-left-color:${attr(e.color || '#7c5cbf')};">`;
            if (e.author || e.authorIcon) {
                const icon = imgSrc(e.authorIcon, data);
                const inner = (icon
                        ? `<img src="${attr(icon)}" alt="" style="${AUTHOR_ICON_STYLE}">`
                        : '')
                    + esc(e.author);
                const href = safeHref(e.authorUrl);
                html += `<div class="eb-pe-author">`
                    + (href
                        ? `<a href="${attr(href)}" target="_blank" rel="noopener noreferrer" style="color:inherit;text-decoration:none;display:flex;align-items:center;gap:6px;">${inner}</a>`
                        : inner)
                    + `</div>`;
            }
            if (e.title) {
                const href = safeHref(e.url);
                const title = esc(e.title);
                html += `<div class="eb-pe-title">`
                    + (href
                        ? `<a href="${attr(href)}" target="_blank" rel="noopener noreferrer" style="color:inherit;text-decoration:none;">${title}</a>`
                        : title)
                    + `</div>`;
            }
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
            const imageSrc = imgSrc(e.image, data);
            if (imageSrc) html += `<img class="eb-pe-image" src="${attr(imageSrc)}" alt="">`;
            const thumbSrc = imgSrc(e.thumbnail, data);
            if (thumbSrc) html += `<img class="eb-pe-thumb" src="${attr(thumbSrc)}" alt="">`;
            const footerIconSrc = imgSrc(e.footerIcon, data);
            const stamp = fmtDiscordTimestamp(e.timestamp);
            if (e.footer || footerIconSrc || stamp) {
                // Discord draws the footer as: icon, text, timestamp — the
                // last two separated by a dot. Same order here.
                const bits = [];
                if (footerIconSrc) bits.push(`<img src="${attr(footerIconSrc)}" alt="" style="${FOOTER_ICON_STYLE}">`);
                if (e.footer) bits.push(`<span>${esc(e.footer)}</span>`);
                if (stamp) bits.push(`<span>${esc(stamp)}</span>`);
                html += `<div class="eb-pe-footer">`
                    + bits.join('<span style="opacity:.6;">&bull;</span>')
                    + `</div>`;
            }
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
    // Editor shape → Discord payload. Every field Discord supports that
    // this editor can set is emitted here, and ONLY when set — an empty
    // string must not become `"footer": {"text": ""}`, which Discord
    // rejects, and must not become a key that never round-trips.
    //
    // The four fields that used to be dropped on this path (author icon,
    // author url, footer icon, embed url) plus `timestamp` are the reason
    // a saved template could never hold them: /embedbuilder/send and
    // /embedbuilder/template/save both serialise whatever this returns.
    function cleanEmbedForPayload(e) {
        const out = {};
        if (e.title) out.title = e.title;
        if (e.description) out.description = e.description;
        if (e.color) { try { out.color = parseInt(e.color.replace('#', ''), 16); } catch (err) {} }
        const author = {};
        if (e.author) author.name = e.author;
        if (e.authorIcon) author.icon_url = e.authorIcon;
        if (e.authorUrl) author.url = e.authorUrl;
        if (Object.keys(author).length) out.author = author;
        const footer = {};
        if (e.footer) footer.text = e.footer;
        if (e.footerIcon) footer.icon_url = e.footerIcon;
        if (Object.keys(footer).length) out.footer = footer;
        if (e.url) out.url = e.url;
        if (e.timestamp) out.timestamp = e.timestamp;
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
            // author/footer are objects in the API shape; the editor keeps
            // each part as its own string so it can round-trip through
            // cleanEmbedForPayload unchanged. Reading only `.name`/`.text`
            // here is what silently threw away the icons and the author
            // link on every save → load → save cycle.
            //
            // The `_icon` fallbacks are for the LEGACY flat shape that
            // embed_templates rows written before this change (and by the
            // older save path) still hold: {author: 'name', author_icon:
            // 'url', footer: 'text', footer_icon: 'url'} — the same keys
            // cogs/embedbuilder.py's build_embed reads. Without them the
            // icons of every pre-existing saved template were dropped the
            // moment the template was loaded into the editor.
            authorIcon: (e.author && e.author.icon_url) || e.author_icon || '',
            authorUrl: (e.author && e.author.url) || e.author_url || '',
            footer: (e.footer && e.footer.text) || e.footer || '',
            footerIcon: (e.footer && e.footer.icon_url) || e.footer_icon || '',
            url: e.url || '',
            // Stored as ISO (what Discord wants); the editor input converts
            // to/from local time and never shows an invalid value.
            timestamp: e.timestamp || '',
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
