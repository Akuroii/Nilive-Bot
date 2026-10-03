// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed-builder-page.js
//
// Everything that is specific to the Embed Builder page: the content
// card + toolbar, the mention modal, the emoji popover, attachments,
// the IndexedDB draft, undo/redo, send and saved templates.
//
// This used to be a ~1000-line `<script>` block inside
// manage/embedbuilder.html, emitted at the bottom of `{% block content %}`
// — which meant Jinja rendered it TWICE (a nested block renders where the
// child puts it AND where the parent declares it), so a hard page load ran
// it twice: two copies of the page state, two listeners per element, two
// IndexedDB restores. On an htmx navigation the fragment's script was
// re-created dynamically and its `<script src>` (embed-composer.js) loaded
// asynchronously, so the inline code below it ran before
// `window.EmbedComposer` existed and threw on the first line — the builder
// came back inert until a hard refresh.
//
// So the page is now a MODULE, not a script block:
//
//   * it lives in an external file (loaded once, by name, by
//     nav-lifecycle.js — see `data-page-script` on the page root),
//   * nothing at the top level touches the DOM; everything happens in
//     `init(root, ctx)`, which the registry calls once per mount,
//   * every listener, timer, blob URL and fetch goes through `ctx`, so
//     `destroy()` cannot leave anything behind,
//   * first paint is SYNCHRONOUS: no network, no IndexedDB, nothing to
//     await before the user sees the composer. Identity comes from the
//     page (`window.__BOT_IDENTITY__`, server-rendered — no Discord call
//     in the init path); limits, lookups and the stored draft arrive
//     after paint and re-render only what they change.
//
// Depends on: embed-composer.js (window.EmbedComposer) and the dashboard
// globals from dashboard.js (showToast/setLoading/showConfirm/checkIconHtml).
// ═══════════════════════════════════════════════════════════════
(function () {
    'use strict';

    var NERO = window.NERO;
    if (!NERO || typeof NERO.definePage !== 'function') {
        // Without the registry this page must not half-initialise: the old
        // failure mode was exactly a half-run script.
        if (window.console) console.error('[embed-builder] nav-lifecycle.js is not loaded');
        return;
    }

    // ═══════════════════════════════════════════════════════════════
    // PURE HELPERS (no DOM, no page state — unit-tested directly by
    // scripts/test_embed_builder_boot.js in the node harness)
    // ═══════════════════════════════════════════════════════════════
    var IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|avif|bmp|ico|tiff?|heic|heif|apng|svg)$/i;
    var IMAGE_MIME_BY_EXT = {
        png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
        webp: 'image/webp', avif: 'image/avif', bmp: 'image/bmp', ico: 'image/x-icon',
        tif: 'image/tiff', tiff: 'image/tiff', heic: 'image/heic', heif: 'image/heif',
        apng: 'image/apng', svg: 'image/svg+xml',
    };

    // Fallbacks for everything the limits endpoint returns. These are the
    // values this page used to hard-code; they are now only a safety net
    // for "the limits call failed" (offline, 500, session expired) — the
    // server's table always wins when it arrives. Kept in ONE object so
    // there is exactly one place to look when a number changes.
    var DEFAULT_LIMITS = {
        contentMax: 2000,
        embedsMax: 10,
        embedTitleMax: 256,
        embedDescriptionMax: 4096,
        embedFieldsMax: 25,
        embedFooterTextMax: 2048,
        embedAuthorNameMax: 256,
        attachmentsMax: 10,
        attachmentTotalBytes: 25 * 1024 * 1024 - 64 * 1024,
        attachmentFileAdvisoryBytes: 20 * 1024 * 1024,
    };

    function limitsFrom(served) {
        // Flat shape so call sites read `L.embedsMax`, not
        // `L.message.embeds_max` — one translation, here.
        var out = {};
        Object.keys(DEFAULT_LIMITS).forEach(function (k) { out[k] = DEFAULT_LIMITS[k]; });
        if (!served || typeof served !== 'object') return out;
        var m = served.message || {}, e = served.embed || {}, a = served.attachments || {};
        function take(key, value) {
            if (typeof value === 'number' && isFinite(value) && value > 0) out[key] = value;
        }
        take('contentMax', m.content_max);
        take('embedsMax', m.embeds_max);
        take('embedTitleMax', e.title_max);
        take('embedDescriptionMax', e.description_max);
        take('embedFieldsMax', e.fields_max);
        take('embedFooterTextMax', e.footer_text_max);
        take('embedAuthorNameMax', e.author_name_max);
        take('attachmentsMax', a.count_max);
        take('attachmentTotalBytes', a.total_bytes_max);
        take('attachmentFileAdvisoryBytes', a.file_bytes_advisory);
        return out;
    }

    // Counter labels: the real cap is 25MiB minus the multipart envelope
    // (24.94MB), which reads as noise in "0B / 24.94MB". Round for display,
    // keep the exact bytes for the decisions.
    function mbLabel(n) {
        if (!isFinite(n) || n <= 0) return '0MB';
        return Math.round(n / 1024 / 1024) + 'MB';
    }

    function humanBytes(n) {
        if (!isFinite(n) || n < 0) return '?';
        if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + 'MB';
        if (n >= 1024) return (n / 1024).toFixed(1) + 'KB';
        return Math.round(n) + 'B';
    }

    /**
     * Should this file be added, and with what caveat?
     * limits: the merged limits object; current: {count, bytes}.
     */
    function classifyFile(size, name, limits, current) {
        if (current.count >= limits.attachmentsMax) {
            return { accepted: false, stop: true,
                     message: 'Max ' + limits.attachmentsMax + ' attachments' };
        }
        if (current.bytes + size > limits.attachmentTotalBytes) {
            return { accepted: false, stop: true,
                     message: 'Attachments would exceed the '
                         + humanBytes(limits.attachmentTotalBytes) + ' total upload limit' };
        }
        var warning = null;
        if (size > limits.attachmentFileAdvisoryBytes) {
            // Advisory: Discord's per-file cap depends on the server's boost
            // level and the account's Nitro tier, so this warns and still
            // lets the file through — Discord's own answer decides.
            warning = 'Single files over '
                + humanBytes(limits.attachmentFileAdvisoryBytes)
                + ' may be refused by Discord on a non-boosted server.';
        }
        return { accepted: true, stop: false, warning: warning, message: '' };
    }

    function looksLikeImage(a) {
        if (!a) return false;
        var t = String(a.type || '');
        if (t.indexOf('image/') === 0) return true;
        if (t) return false;                      // a real, non-image MIME wins
        return IMAGE_EXT_RE.test(String(a.name || ''));
    }

    function imageMimeFor(name) {
        var m = /\.([a-z0-9]+)$/i.exec(String(name || ''));
        return (m && IMAGE_MIME_BY_EXT[m[1].toLowerCase()]) || '';
    }

    function resolveIdentity(injected, fallbackName) {
        var src = (injected && typeof injected === 'object') ? injected : {};
        var name = src.name || fallbackName || 'Nero';
        return {
            name: name,
            avatar: src.avatar || null,
            source: src.name ? (src.source || 'page') : 'default',
        };
    }

    // datetime-local ⇄ ISO. Discord stores/serves `timestamp` as an ISO
    // string; the input needs local wall-clock time.
    function isoToLocalInput(iso) {
        if (!iso) return '';
        var d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        var pad = function (n) { return String(n).padStart(2, '0'); };
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
            + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }
    function localInputToIso(local) {
        if (!local) return '';
        var d = new Date(local);
        if (isNaN(d.getTime())) return '';
        return d.toISOString();
    }

    // Extra editor fields (additive to embed-composer's mountEditor). Only
    // fields Discord actually renders are added — no placeholders for
    // features that do not exist, and no Components V2 fields.
    var EXTRA_FIELDS = [
        { key: 'authorIcon', label: 'Author Icon URL', placeholder: 'https://…', type: 'text' },
        { key: 'authorUrl', label: 'Author Link', placeholder: 'https://…', type: 'text' },
        { key: 'footerIcon', label: 'Footer Icon URL', placeholder: 'https://…', type: 'text' },
        { key: 'url', label: 'Title Link (embed URL)', placeholder: 'https://…', type: 'text' },
        { key: 'timestamp', label: 'Timestamp', type: 'datetime-local' },
    ];

    NERO.embedBuilder = {
        version: 1,
        helpers: {
            limitsFrom: limitsFrom,
            humanBytes: humanBytes,
            mbLabel: mbLabel,
            classifyFile: classifyFile,
            looksLikeImage: looksLikeImage,
            imageMimeFor: imageMimeFor,
            resolveIdentity: resolveIdentity,
            isoToLocalInput: isoToLocalInput,
            localInputToIso: localInputToIso,
            DEFAULT_LIMITS: DEFAULT_LIMITS,
            EXTRA_FIELDS: EXTRA_FIELDS,
        },
    };

    // ═══════════════════════════════════════════════════════════════
    // THE PAGE MODULE
    // ═══════════════════════════════════════════════════════════════
    NERO.definePage('embed-builder', {
        init: function (root, ctx) {
            var EC = window.EmbedComposer;
            if (!EC) throw new Error('embed-composer.js did not load');

            var _ebEsc = EC.esc;
            var _ebAttr = EC.attr;
            var insertAtCursor = EC.insertAtCursor;
            var wrapSelection = EC.wrapSelection;
            var blankEmbed = EC.blankEmbed;

            function need(id) {
                var node = root.querySelector('#' + id);
                if (!node) throw new Error('embed-builder: missing #' + id + ' in the page');
                return node;
            }
            function now() {
                if (window.performance && typeof window.performance.now === 'function') {
                    return window.performance.now();
                }
                return Date.now();
            }

            // ── Limits: defaults now, the server's table after paint ──
            var LIMITS = limitsFrom(null);
            var IDB_NAME = 'nero_embedbuilder';
            var IDB_STORE = 'draft';
            var IDB_TIMEOUT_MS = 1500;
            var DRAFT_RESTORE_TIMEOUT_MS = 3000;
            var DRAFT_DEBOUNCE_MS = 500;
            var HISTORY_DEBOUNCE_MS = 350;
            var EMOJI_SEARCH_DEBOUNCE_MS = 150;

            // ── State (per mount: a remount starts clean) ────────────
            var state = { content: '', embeds: [blankEmbed()] };
            var attachments = [];   // { id, name, type, size, blob, source, ... }
            var activeEmbed = 0;
            var history = [];
            var historyIndex = -1;
            var suppressHistory = false;
            var dirty = false;      // true once the user edits anything
            var idbState = 'unknown';   // unknown | ready | unavailable
            var idbError = '';
            var _idb = null;
            var customEmojiCache = null;
            var externalEmojiCache = null;
            var externalEmojiExpanded = false;
            var appEmojiCache = null;
            var botIdentity = resolveIdentity(window.__BOT_IDENTITY__, 'Nero');
            var roleMap = {}, channelMap = {}, userNameCache = {};
            var userResolving = {};
            var savedSelection = { start: 0, end: 0 };
            var emojiInsertTarget = null;
            var _previewRefreshQueued = false;

            // ── Element refs ─────────────────────────────────────────
            var contentInput = need('eb-content');
            var contentCounter = need('eb-content-counter');
            var attachGrid = need('eb-attach-grid');
            var attachCounter = need('eb-attach-counter');
            var dropzone = need('eb-dropzone');
            var fileInput = need('eb-file-input');
            var embedsList = need('eb-embeds-list');
            var embedCounter = need('eb-embed-counter');
            var previewBox = need('eb-preview');
            var mentionModal = need('eb-mention-modal');
            var emojiPopover = need('eb-emoji-popover');
            var emojiScroll = need('eb-emoji-scroll');
            var emojiSearch = need('eb-emoji-search');
            var emojiHoverBar = need('eb-emoji-hover-bar');
            var statusEl = need('eb-status');
            var sendBtn = need('eb-send-btn');
            var templateSelect = need('eb-template-select');
            var undoBtn = need('eb-undo-btn');
            var redoBtn = need('eb-redo-btn');

            function toast(msg, kind) { if (window.showToast) window.showToast(msg, kind); }
            function confirmThen(message, fn) {
                if (window.showConfirm) return window.showConfirm(message, fn);
                if (window.confirm && window.confirm(message)) return fn();
                return undefined;
            }

            // ═══════════════════════════════════════════════════════
            // STATUS LINE
            // setStatus: transient (auto-clears). setStatusPersistent:
            // stays until something replaces it — used for the degraded
            // modes (draft storage unavailable) that must not vanish
            // after four seconds, because they stay true.
            // ═══════════════════════════════════════════════════════
            var statusTimer = null;
            function setStatus(msg, ok) {
                if (statusTimer) { ctx.clearTimeout(statusTimer); statusTimer = null; }
                if (ok && window.checkIconHtml) {
                    statusEl.innerHTML = window.checkIconHtml() + ' ' + _ebEsc(msg);
                } else {
                    statusEl.textContent = msg;
                }
                statusEl.style.color = ok ? 'var(--success)' : 'var(--danger)';
                statusTimer = ctx.timeout(function () { statusEl.textContent = ''; statusTimer = null; }, 4000);
            }
            function setStatusPersistent(msg, kind) {
                if (statusTimer) { ctx.clearTimeout(statusTimer); statusTimer = null; }
                statusEl.textContent = msg;
                statusEl.style.color = kind === 'warn' ? 'var(--warning, #d9a253)' : 'var(--text3)';
            }

            // ═══════════════════════════════════════════════════════
            // INDEXEDDB DRAFT — never able to blank the page
            //
            // The old code awaited `restoreDraft()` before its first
            // render. When IndexedDB is blocked (private mode, a second
            // tab holding an upgrade, storage disabled) the open request
            // never settles, so that await never resolved — and the
            // builder stayed an empty shell forever with no error and no
            // explanation. Now: open with a timeout, mark the page
            // degraded on any failure, keep the composer fully usable,
            // and say so in the status line.
            // ═══════════════════════════════════════════════════════
            function markIdbUnavailable(reason, message) {
                if (idbState === 'unavailable') return;
                idbState = 'unavailable';
                idbError = reason;
                ctx.counter('idbUnavailable');
                ctx.debug('idb-unavailable', reason);
                setStatusPersistent(message || ('Draft autosave is off — this browser blocked local '
                    + 'storage. Everything else works, but a refresh will not restore your work.'), 'warn');
            }

            function openIdb() {
                return new Promise(function (resolve, reject) {
                    if (_idb) return resolve(_idb);
                    if (idbState === 'unavailable') return reject(new Error(idbError || 'unavailable'));
                    var req;
                    try {
                        req = indexedDB.open(IDB_NAME, 1);
                    } catch (e) {
                        markIdbUnavailable('open-threw', 'Draft autosave is off — this browser '
                            + 'does not allow local storage here. Everything else works.');
                        return reject(e);
                    }
                    var settled = false;
                    var timer = ctx.timeout(function () {
                        if (settled) return;
                        settled = true;
                        markIdbUnavailable('open-timeout');
                        reject(new Error('IndexedDB open timed out'));
                    }, IDB_TIMEOUT_MS);
                    function done(ok, value) {
                        if (settled) return;
                        settled = true;
                        ctx.clearTimeout(timer);
                        ok ? resolve(value) : reject(value);
                    }
                    req.onupgradeneeded = function () {
                        try { req.result.createObjectStore(IDB_STORE); } catch (e) { /* exists */ }
                    };
                    req.onsuccess = function () { _idb = req.result; idbState = 'ready'; done(true, _idb); };
                    req.onerror = function () {
                        markIdbUnavailable('open-error');
                        done(false, req.error || new Error('IndexedDB open failed'));
                    };
                    // onblocked: another tab is holding an older version
                    // open. Do not spin — the timeout above is the answer.
                    req.onblocked = function () { ctx.debug('idb-blocked'); };
                });
            }

            function idbSet(key, value) {
                if (idbState === 'unavailable') return Promise.resolve(false);
                return openIdb().then(function (db) {
                    return new Promise(function (resolve) {
                        var tx = db.transaction(IDB_STORE, 'readwrite');
                        tx.objectStore(IDB_STORE).put(value, key);
                        tx.oncomplete = function () { resolve(true); };
                        tx.onerror = function () {
                            markIdbUnavailable('write-error');
                            resolve(false);
                        };
                    });
                }).catch(function () { return false; });
            }

            function idbGet(key) {
                if (idbState === 'unavailable') return Promise.resolve(null);
                return openIdb().then(function (db) {
                    return new Promise(function (resolve) {
                        try {
                            var tx = db.transaction(IDB_STORE, 'readonly');
                            var req = tx.objectStore(IDB_STORE).get(key);
                            req.onsuccess = function () { resolve(req.result); };
                            req.onerror = function () { resolve(null); };
                        } catch (e) {
                            resolve(null);
                        }
                    });
                }).catch(function () { return null; });
            }

            function idbClear() {
                if (idbState === 'unavailable') return Promise.resolve(false);
                return openIdb().then(function (db) {
                    return new Promise(function (resolve) {
                        var tx = db.transaction(IDB_STORE, 'readwrite');
                        tx.objectStore(IDB_STORE).clear();
                        tx.oncomplete = function () { resolve(true); };
                        tx.onerror = function () { resolve(false); };
                    });
                }).catch(function () { return false; });
            }

            // ═══════════════════════════════════════════════════════
            // DEBOUNCED WORK — both timers live in the page context, so
            // leaving the page cancels them instead of firing on a dead DOM
            // ═══════════════════════════════════════════════════════
            function makeDebounced(fn, ms) {
                var timer = null;
                var wrapped = function () {
                    var args = arguments;
                    if (timer) ctx.clearTimeout(timer);
                    timer = ctx.timeout(function () {
                        timer = null;
                        fn.apply(null, args);
                    }, ms);
                };
                wrapped.cancel = function () {
                    if (timer) { ctx.clearTimeout(timer); timer = null; }
                };
                return wrapped;
            }

            var saveDraft = makeDebounced(function () {
                if (idbState === 'unavailable') return;   // degraded: no writes, no console spam
                var t0 = now();
                idbSet('composer', {
                    content: state.content,
                    embeds: state.embeds,
                    attachments: attachments.map(function (a) {
                        return {
                            id: a.id, name: a.name, type: a.type, size: a.size,
                            blob: a.blob, source: a.source || 'local',
                        };
                    }),
                    ts: Date.now(),
                }).then(function (ok) {
                    if (ok) ctx.counters.draftWrites = (ctx.counters.draftWrites || 0) + 1;
                    ctx.debug('draft-write', Math.round(now() - t0));
                });
            }, DRAFT_DEBOUNCE_MS);

            var pushHistory = makeDebounced(function () {
                if (suppressHistory) return;
                var snap = JSON.stringify({ content: state.content, embeds: state.embeds });
                if (history[historyIndex] === snap) return;
                history = history.slice(0, historyIndex + 1);
                history.push(snap);
                if (history.length > 60) history.shift();
                historyIndex = history.length - 1;
                updateUndoRedoButtons();
            }, HISTORY_DEBOUNCE_MS);

            function snapshotForHistory() {
                return JSON.stringify({ content: state.content, embeds: state.embeds });
            }

            function updateUndoRedoButtons() {
                undoBtn.disabled = historyIndex <= 0;
                redoBtn.disabled = historyIndex >= history.length - 1;
            }

            function applySnapshot(snap) {
                var parsed = JSON.parse(snap);
                suppressHistory = true;
                state.content = parsed.content;
                state.embeds = parsed.embeds;
                activeEmbed = Math.min(activeEmbed, state.embeds.length - 1);
                renderAll();
                suppressHistory = false;
            }

            function undo() {
                if (historyIndex <= 0) return;
                historyIndex--;
                applySnapshot(history[historyIndex]);
                updateUndoRedoButtons();
            }
            function redo() {
                if (historyIndex >= history.length - 1) return;
                historyIndex++;
                applySnapshot(history[historyIndex]);
                updateUndoRedoButtons();
            }

            // ═══════════════════════════════════════════════════════
            // CONTENT + TOOLBAR
            // ═══════════════════════════════════════════════════════
            function updateContentCounter() {
                var len = contentInput.value.length;
                contentCounter.textContent = len + ' / ' + LIMITS.contentMax;
                contentCounter.classList.toggle('eb-counter-warn',
                    len > LIMITS.contentMax * 0.85 && len <= LIMITS.contentMax);
                contentCounter.classList.toggle('eb-counter-danger', len >= LIMITS.contentMax);
            }

            function onContentEdited() {
                dirty = true;
                state.content = contentInput.value;
                updateContentCounter();
                renderPreview();
                saveDraft();
                pushHistory();
            }

            ctx.on(contentInput, 'input', onContentEdited);

            root.querySelectorAll('.eb-tbtn[data-fmt]').forEach(function (btn) {
                ctx.on(btn, 'click', function () {
                    var map = { bold: '**', italic: '*', underline: '__', strike: '~~' };
                    wrapSelection(contentInput, map[btn.dataset.fmt]);
                    onContentEdited();
                });
            });

            function captureSelection() {
                savedSelection = { start: contentInput.selectionStart, end: contentInput.selectionEnd };
            }
            function restoreSelectionAndInsert(text) {
                contentInput.focus();
                contentInput.setSelectionRange(savedSelection.start, savedSelection.end);
                insertAtCursor(contentInput, text);
                onContentEdited();
            }

            // ═══════════════════════════════════════════════════════
            // MENTION MODAL
            // ═══════════════════════════════════════════════════════
            function closeMentionModal() {
                mentionModal.style.display = 'none';
                contentInput.focus();
            }
            ctx.on(need('eb-mention-btn'), 'click', function () {
                captureSelection();
                mentionModal.style.display = 'flex';
            });
            ctx.on(need('eb-mention-cancel'), 'click', closeMentionModal);
            ctx.on(mentionModal, 'click', function (e) {
                if (e.target === mentionModal) closeMentionModal();
            });
            ctx.on(need('eb-mention-channel-btn'), 'click', function () {
                var id = pickerValue('#eb-mention-channel');
                if (!id) { toast('Pick a channel', 'warning'); return; }
                restoreSelectionAndInsert('<#' + id + '>');
                closeMentionModal();
            });
            ctx.on(need('eb-mention-role-btn'), 'click', function () {
                var id = pickerValue('#eb-mention-role');
                if (!id) { toast('Pick a role', 'warning'); return; }
                restoreSelectionAndInsert('<@&' + id + '>');
                closeMentionModal();
            });
            ctx.on(need('eb-mention-user-btn'), 'click', function () {
                var input = need('eb-mention-user');
                var val = input.value.trim();
                if (!/^\d{17,20}$/.test(val)) { toast('Enter a valid Discord user ID', 'warning'); return; }
                restoreSelectionAndInsert('<@' + val + '>');
                input.value = '';
                closeMentionModal();
            });
            // select2 rewrites the value into its own widget, so read it the
            // way the old page did (jQuery) and fall back to the raw element.
            function pickerValue(selector) {
                var node = root.querySelector(selector);
                if (window.$ && window.$.fn && node) {
                    try { return window.$(node).val() || ''; } catch (e) { /* fall through */ }
                }
                return node ? node.value : '';
            }

            // ═══════════════════════════════════════════════════════
            // EMOJI POPOVER
            //
            // The grid used to bind three listeners per CELL (mouseenter,
            // mouseleave, click — 846 listeners for the 282 unicode emoji
            // alone) and re-render the whole grid on every keystroke of the
            // search box. Now: three delegated listeners on the scroll
            // container (bounded, independent of cell count) and a debounced
            // filter — invariant P9 in the plan.
            // ═══════════════════════════════════════════════════════
            var UNICODE_EMOJI = {
                'Frequently Used': [],
                'Smileys': ['😀','😁','😂','🤣','😊','😇','🙂','🙃','😉','😌','😍','🥰','😘','😗','😋','😛','😜','🤪','🤑','🤗','🤔','🤨','😐','😑','😶','🙄','😏','😣','😥','😮','🤐','😯','😪','😫','🥱','😴','🤤','😒','😓','😔','😕','🫠','🥲','😢','😭','😤','😠','😡','🤬','🤯','😳','🥵','🥶','😱','😨','😰'],
                'People': ['👋','🤚','🖐️','✋','🖖','👌','🤌','🤏','✌️','🤞','🫰','🤟','🤘','🤙','👈','👉','👆','👇','☝️','👍','👎','✊','👊','🤛','🤜','👏','🙌','🫶','👐','🤲','🙏','💪','🦾','🫵','🧠','👀','👁️','👶','🧒','👦','👧','🧑','👨','👩','🧓','👴','👵'],
                'Animals': ['🐶','🐱','🐭','🐹','🐰','🦊','🐻','🐼','🐨','🐯','🦁','🐮','🐷','🐸','🐵','🙈','🙉','🙊','🐔','🐧','🐦','🐤','🦆','🦅','🦉','🦇','🦺','🐺','🐗','🐴','🦄','🐝','🐛','🦋','🐌','🐞','🐢','🐍','🦎','🦖','🐙','🐳','🐬','🐟','🐠'],
                'Food': ['🍏','🍎','🍐','🍊','🍋','🍌','🍉','🍇','🍓','🫐','🍈','🍒','🍑','🥭','🍍','🥥','🥝','🍅','🍆','🥑','🥦','🌽','🥕','🥐','🍞','🥖','🧀','🍗','🍔','🍟','🍕','🌭','🥪','🌮','🌯','🍜','🍣','🍱','🍰','🎂','🍩','🍪','🍫','🍿','🍵','☕'],
                'Objects': ['⌚','📱','💻','⌨️','🖥️','🖨️','📷','🎥','📞','📺','🎮','🕹️','💡','🔦','📔','📚','📝','✏️','🖊️','📌','📎','✂️','🔒','🔑','🔨','🧰','⚙️','🧲','🎁','🎈','🎉','🎊','🏆','🥇','🎖️'],
                'Symbols': ['❤️','🧡','💛','💚','💙','💜','🖤','🤍','🤎','💔','❣️','💕','💞','💓','💗','💖','💘','✨','⭐','🌟','💫','⚡','🔥','💥','💯','✅','❌','❗','❓','💤','🔔','🔕','🚫','⚠️','♻️','🆗','🆕','🆒','🔞'],
                'Flags': ['🏁','🚩','🎌','🏴','🏳️','🏳️‍🌈'],
            };

            function loadCustomEmojis() {
                if (customEmojiCache) return Promise.resolve(customEmojiCache);
                return ctx.fetchJSON('/api/guild/emojis').then(function (data) {
                    customEmojiCache = data.results || [];
                    return customEmojiCache;
                }, function () {
                    customEmojiCache = [];
                    ctx.counter('emojiLoadFailures');
                    return customEmojiCache;
                });
            }

            function loadAppEmojis(force) {
                if (appEmojiCache && !force) return Promise.resolve(appEmojiCache);
                return ctx.fetchJSON('/api/app-emojis').then(function (data) {
                    appEmojiCache = data.results || [];
                    return appEmojiCache;
                }, function () {
                    appEmojiCache = appEmojiCache || [];
                    return appEmojiCache;
                });
            }

            // External emojis (the bot's OTHER servers) are an N-guild
            // fan-out on the backend, fetched only when explicitly expanded.
            function loadExternalEmojis() {
                if (externalEmojiCache) return Promise.resolve(externalEmojiCache);
                return ctx.fetchJSON('/api/guild/emojis/external').then(function (data) {
                    externalEmojiCache = data.servers || [];
                    return externalEmojiCache;
                }, function () {
                    externalEmojiCache = [];
                    return externalEmojiCache;
                });
            }

            function emojiUrl(id, animated) {
                return 'https://cdn.discordapp.com/emojis/' + id + '.' + (animated ? 'gif' : 'png');
            }

            function loadFrequentEmojis() {
                try { return JSON.parse(window.localStorage.getItem('nero_eb_freq_emoji') || '[]'); }
                catch (e) { return []; }
            }
            function bumpFrequentEmoji(token) {
                try {
                    var freq = loadFrequentEmojis();
                    var idx = freq.findIndex(function (f) { return f.token === token.token; });
                    if (idx >= 0) freq[idx].count++;
                    else freq.push(Object.assign({}, token, { count: 1 }));
                    freq.sort(function (a, b) { return b.count - a.count; });
                    window.localStorage.setItem('nero_eb_freq_emoji', JSON.stringify(freq.slice(0, 16)));
                } catch (e) { /* storage unavailable — frequent emojis are a nicety */ }
            }

            function openEmojiPopover(anchorBtn, targetTextarea) {
                emojiInsertTarget = targetTextarea;
                if (targetTextarea === contentInput) captureSelection();
                Promise.all([loadCustomEmojis(), loadAppEmojis()]).then(function () {
                    if (ctx.isDestroyed()) return;
                    emojiSearch.value = '';
                    need('eb-emoji-import-input').value = '';
                    resetEmojiHoverBar();
                    renderEmojiGrid('');
                    var rect = anchorBtn.getBoundingClientRect();
                    var popW = 340;
                    emojiPopover.style.top = (rect.bottom + 6) + 'px';
                    emojiPopover.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - popW - 8)) + 'px';
                    emojiPopover.style.display = 'flex';
                    emojiSearch.focus();
                });
            }
            function closeEmojiPopover() {
                emojiPopover.style.display = 'none';
                emojiInsertTarget = null;
            }

            ctx.on(need('eb-emoji-btn'), 'click', function (e) {
                openEmojiPopover(e.currentTarget, contentInput);
            });
            ctx.on(document, 'click', function (e) {
                if (emojiPopover.style.display === 'none') return;
                if (emojiPopover.contains(e.target)) return;
                if (e.target.id === 'eb-emoji-btn') return;
                if (e.target.closest && e.target.closest('.eb-emoji-embed-btn')) return;
                closeEmojiPopover();
            });

            function resetEmojiHoverBar() {
                emojiHoverBar.innerHTML = '<span class="eb-emoji-hover-empty">Pick an emoji…</span>';
            }

            function emojiCell(display, token, name) {
                return '<button type="button" class="eb-emoji-cell" data-token="' + _ebAttr(token)
                    + '" data-name="' + _ebAttr(name || '') + '">' + display + '</button>';
            }

            function renderEmojiGrid(query) {
                var q = String(query || '').trim().toLowerCase();
                var freq = loadFrequentEmojis();
                var html = '';

                if (!q && freq.length) {
                    html += '<div class="eb-emoji-cat-label">Frequently Used</div><div class="eb-emoji-grid">';
                    html += freq.map(function (f) {
                        return f.img
                            ? emojiCell('<img src="' + _ebEsc(f.img) + '" alt="">', f.token, f.name)
                            : emojiCell(_ebEsc(f.char), f.token, '');
                    }).join('');
                    html += '</div>';
                }

                Object.keys(UNICODE_EMOJI).forEach(function (cat) {
                    if (cat === 'Frequently Used') return;
                    var list = UNICODE_EMOJI[cat];
                    if (q && cat.toLowerCase().indexOf(q) === -1) return;  // no per-glyph names to search
                    if (!list.length) return;
                    html += '<div class="eb-emoji-cat-label">' + _ebEsc(cat) + '</div><div class="eb-emoji-grid">';
                    html += list.map(function (ch) { return emojiCell(_ebEsc(ch), ch, ''); }).join('');
                    html += '</div>';
                });

                var custom = (customEmojiCache || []).filter(function (e) {
                    return !q || e.name.toLowerCase().indexOf(q) !== -1;
                });
                if (custom.length) {
                    html += '<div class="eb-emoji-cat-label">Custom Emojis</div><div class="eb-emoji-grid">';
                    html += custom.map(function (e) {
                        var token = e.animated ? '<a:' + e.name + ':' + e.id + '>' : '<:' + e.name + ':' + e.id + '>';
                        return emojiCell('<img src="' + emojiUrl(e.id, e.animated) + '" alt="' + _ebEsc(e.name) + '">',
                            token, e.name);
                    }).join('');
                    html += '</div>';
                }

                var appEmojis = (appEmojiCache || []).filter(function (e) {
                    return !q || e.name.toLowerCase().indexOf(q) !== -1;
                });
                if (appEmojis.length) {
                    html += '<div class="eb-emoji-cat-label">App Emojis</div><div class="eb-emoji-grid">';
                    html += appEmojis.map(function (e) {
                        var token = e.animated ? '<a:' + e.name + ':' + e.id + '>' : '<:' + e.name + ':' + e.id + '>';
                        return emojiCell('<img src="' + emojiUrl(e.id, e.animated) + '" alt="' + _ebEsc(e.name) + '">',
                            token, e.name);
                    }).join('');
                    html += '</div>';
                }

                if (externalEmojiExpanded || q) {
                    if (externalEmojiCache) {
                        externalEmojiCache.forEach(function (server) {
                            var matched = server.emojis.filter(function (e) {
                                return !q || e.name.toLowerCase().indexOf(q) !== -1;
                            });
                            if (!matched.length) return;
                            html += '<div class="eb-emoji-server-label">🌐 ' + _ebEsc(server.guild_name)
                                + '</div><div class="eb-emoji-grid">';
                            html += matched.map(function (e) {
                                var token = e.animated ? '<a:' + e.name + ':' + e.id + '>' : '<:' + e.name + ':' + e.id + '>';
                                return emojiCell('<img src="' + emojiUrl(e.id, e.animated) + '" alt="' + _ebEsc(e.name) + '">',
                                    token, e.name + ' · ' + server.guild_name);
                            }).join('');
                            html += '</div>';
                        });
                    }
                } else {
                    html += '<button type="button" class="eb-emoji-ext-toggle" data-emoji-ext-toggle>'
                        + '🌐 Show emojis from other servers</button>';
                }

                if (!html) html = '<div class="eb-emoji-empty">No emoji matched "' + _ebEsc(query) + '"</div>';
                emojiScroll.innerHTML = html;
                ctx.counter('emojiGridRenders');
            }

            // ── Delegated handlers: three, forever (P9) ──────────────
            ctx.on(emojiScroll, 'click', function (e) {
                var extToggle = e.target.closest && e.target.closest('[data-emoji-ext-toggle]');
                if (extToggle) {
                    extToggle.textContent = 'Loading…';
                    loadExternalEmojis().then(function () {
                        if (ctx.isDestroyed()) return;
                        externalEmojiExpanded = true;
                        renderEmojiGrid(emojiSearch.value);
                    });
                    return;
                }
                var cell = e.target.closest && e.target.closest('.eb-emoji-cell');
                if (!cell) return;
                var token = cell.dataset.token;
                var imgEl = cell.querySelector('img');
                bumpFrequentEmoji(imgEl
                    ? { token: token, img: imgEl.getAttribute('src'), name: cell.dataset.name }
                    : { token: token, char: token, name: '' });
                insertEmojiToken(token);
                closeEmojiPopover();
            });
            ctx.on(emojiScroll, 'mouseover', function (e) {
                var cell = e.target.closest && e.target.closest('.eb-emoji-cell');
                if (!cell) return;
                var imgEl = cell.querySelector('img');
                var name = cell.dataset.name;
                if (imgEl) {
                    emojiHoverBar.innerHTML = '<img src="' + imgEl.getAttribute('src') + '" alt="">'
                        + (name ? '<span class="eb-emoji-hover-name">:' + _ebEsc(name.split(' · ')[0]) + ':</span>' : '');
                } else if (name === '') {
                    emojiHoverBar.innerHTML = '<span class="eb-emoji-hover-name" style="font-size:18px;">'
                        + cell.textContent + '</span>';
                }
            });
            ctx.on(emojiScroll, 'mouseout', function (e) {
                if (e.target.closest && e.target.closest('.eb-emoji-cell')) resetEmojiHoverBar();
            });

            var onEmojiSearch = makeDebounced(function () {
                renderEmojiGrid(emojiSearch.value);
            }, EMOJI_SEARCH_DEBOUNCE_MS);
            ctx.on(emojiSearch, 'input', onEmojiSearch);

            function insertEmojiToken(token) {
                restoreSelectionAndInsert(token);
            }

            ctx.on(need('eb-emoji-import-btn'), 'click', function () {
                var input = need('eb-emoji-import-input');
                var raw = input.value.trim();
                if (!raw) { toast('Paste an emoji ID or its <:name:id> markdown', 'warning'); return; }
                var btn = need('eb-emoji-import-btn');
                if (window.setLoading) window.setLoading(btn, true, '…');
                ctx.fetch('/api/app-emojis/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ raw: raw }),
                }).then(function (res) { return res.json(); }).then(function (data) {
                    if (ctx.isDestroyed()) return;
                    if (data.success) {
                        var token = data.animated
                            ? '<a:' + data.name + ':' + data.id + '>'
                            : '<:' + data.name + ':' + data.id + '>';
                        toast(data.cached ? 'Already imported — inserted!' : 'Imported as an Application Emoji!');
                        return loadAppEmojis(true).then(function () {
                            if (ctx.isDestroyed()) return;
                            input.value = '';
                            insertEmojiToken(token);
                            renderEmojiGrid(emojiSearch.value);
                        });
                    }
                    toast(data.error || 'Import failed', 'error');
                }, function () {
                    if (!ctx.isDestroyed()) toast('Connection error', 'error');
                }).then(function () {
                    if (window.setLoading) window.setLoading(btn, false);
                });
            });

            // ═══════════════════════════════════════════════════════
            // ATTACHMENTS
            // ═══════════════════════════════════════════════════════
            ctx.on(need('eb-file-browse'), 'click', function () { fileInput.click(); });
            ctx.on(dropzone, 'click', function (e) {
                if (e.target === dropzone || e.target.tagName !== 'BUTTON') fileInput.click();
            });
            ctx.on(fileInput, 'change', function () {
                addFiles(fileInput.files);
                fileInput.value = '';
            });
            ['dragenter', 'dragover'].forEach(function (evt) {
                ctx.on(dropzone, evt, function (e) { e.preventDefault(); dropzone.classList.add('eb-dragover'); });
            });
            ['dragleave', 'drop'].forEach(function (evt) {
                ctx.on(dropzone, evt, function (e) { e.preventDefault(); dropzone.classList.remove('eb-dragover'); });
            });
            ctx.on(dropzone, 'drop', function (e) { addFiles(e.dataTransfer.files); });

            function addFiles(fileList) {
                if (!fileList || !fileList.length) return;
                var totalBytes = attachments.reduce(function (s, a) { return s + a.size; }, 0);
                var warned = null;
                for (var i = 0; i < fileList.length; i++) {
                    var f = fileList[i];
                    var verdict = classifyFile(f.size, f.name, LIMITS,
                        { count: attachments.length, bytes: totalBytes });
                    if (!verdict.accepted) {
                        toast(verdict.message, 'warning');
                        if (verdict.stop) break;
                        continue;
                    }
                    totalBytes += f.size;
                    if (verdict.warning) warned = verdict.warning;
                    attachments.push({
                        id: 'att_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
                        name: f.name,
                        type: f.type || (IMAGE_EXT_RE.test(f.name) ? imageMimeFor(f.name) : ''),
                        size: f.size,
                        blob: f,
                        source: 'local',
                        // advisory only — shown on the tile, never blocks
                        _sizeWarning: verdict.warning || null,
                    });
                }
                if (warned) toast(warned, 'warning');
                dirty = true;
                renderAttachments();
                renderPreview();
                saveDraft();
            }

            // ── Attachment preview URLs (one per attachment, revocable) ──
            function attachmentPreviewHint(a) {
                if (a._previewError) {
                    return (EC.NO_PREVIEW_HINT && EC.NO_PREVIEW_HINT[a._previewError]) || '';
                }
                return a.size > (EC.DATA_URL_MAX_BYTES || 0)
                    ? 'Too large to preview inline — it will still be sent.'
                    : 'No preview available — the file will still be sent.';
            }

            function queuePreviewRefresh() {
                if (_previewRefreshQueued) return;
                _previewRefreshQueued = true;
                Promise.resolve().then(function () {
                    _previewRefreshQueued = false;
                    if (ctx.isDestroyed()) return;
                    renderAttachments();
                    renderPreview();
                });
            }

            function refreshAttachmentPreview(a) {
                if (!a || a._previewPending || a._previewUrl || a._dataUrl || a._previewRefreshing) return;
                if (a._previewError) {
                    a._previewError = null;
                    a._previewRefreshing = true;
                    queuePreviewRefresh();
                } else {
                    a._previewPending = true;
                    queuePreviewRefresh();
                }
                var t0 = now();
                EC.previewUrlFor(a).then(function (r) {
                    ctx.counters.imagePreviewResolutions = (ctx.counters.imagePreviewResolutions || 0) + 1;
                    ctx.debug('image-preview', { ms: Math.round(now() - t0), kind: r && r.kind });
                    a._previewPending = false;
                    a._previewRefreshing = false;
                    if (r && r.url) a._previewError = null;
                    else a._previewError = (r && r.reason) || 'no-blob';
                    queuePreviewRefresh();
                });
            }

            function getAttachmentPreviewUrl(a) {
                if (!a) return null;
                if (a.source === 'remote') return a.url || null;
                if (a._previewUrl) return a._previewUrl;
                if (a._dataUrl) return a._dataUrl;
                if (!a.blob || a._previewPending || a._previewError) return null;
                refreshAttachmentPreview(a);
                return null;
            }

            function revokeAttachmentPreview(a) {
                if (!a) return;
                if (a._previewUrl) {
                    try { URL.revokeObjectURL(a._previewUrl); } catch (e) { /* already gone */ }
                }
                a._previewUrl = null;
                a._dataUrl = null;
                a._previewError = null;
                a._previewPending = false;
                a._previewRefreshing = false;
            }

            function revokeAllAttachmentPreviews() {
                attachments.forEach(revokeAttachmentPreview);
            }
            // Teardown: release every blob URL this page minted, even if the
            // user never removed the attachment (the old page leaked them
            // into htmx's history cache until the tab closed).
            ctx.cleanup(revokeAllAttachmentPreviews);

            function removeAttachment(id) {
                var target = attachments.filter(function (a) { return a.id === id; })[0];
                revokeAttachmentPreview(target);
                attachments = attachments.filter(function (a) { return a.id !== id; });
                dirty = true;
                renderAttachments();
                renderPreview();
                saveDraft();
            }

            function renderAttachments() {
                var totalBytes = attachments.reduce(function (s, a) { return s + a.size; }, 0);
                attachCounter.textContent = attachments.length + ' / ' + LIMITS.attachmentsMax
                    + ' · ' + humanBytes(totalBytes) + ' / ' + mbLabel(LIMITS.attachmentTotalBytes);
                attachCounter.classList.toggle('eb-counter-warn',
                    totalBytes > LIMITS.attachmentTotalBytes * 0.8);
                ctx.counter('attachmentRenders');

                attachGrid.innerHTML = attachments.map(function (a) {
                    try {
                        var isImg = looksLikeImage(a);
                        var url = isImg ? getAttachmentPreviewUrl(a) : null;
                        var inner, hint = '';
                        if (isImg && url) {
                            inner = '<img class="eb-attach-thumb" src="' + _ebAttr(url)
                                + '" alt="" loading="lazy" decoding="async">';
                        } else if (isImg && a._previewPending) {
                            inner = '<div class="eb-attach-thumb--pending">⏳ preview…</div>';
                        } else if (isImg) {
                            inner = '<div class="eb-attach-status">🖼️<span>no preview</span>'
                                + '<button type="button" class="eb-attach-retry" data-retry="' + a.id
                                + '">retry</button></div>';
                            hint = '<div class="eb-attach-hint">' + _ebEsc(attachmentPreviewHint(a)) + '</div>';
                        } else {
                            inner = '<div class="eb-attach-icon">📄</div>';
                        }
                        var sizeHint = a._sizeWarning
                            ? '<div class="eb-attach-hint">' + _ebEsc(a._sizeWarning) + '</div>'
                            : '';
                        return '<div class="eb-attach-item">'
                            + '<button type="button" class="eb-attach-remove" data-id="' + a.id
                            + '" aria-label="Remove ' + _ebEsc(a.name) + '">✕</button>'
                            + inner
                            + '<div class="eb-attach-name" title="' + _ebEsc(a.name) + '">' + _ebEsc(a.name) + '</div>'
                            + hint + sizeHint
                            + '</div>';
                    } catch (e) {
                        console.error('[embedbuilder] failed to render attachment', a && a.name, e);
                        return '<div class="eb-attach-item">'
                            + '<button type="button" class="eb-attach-remove" data-id="' + (a && a.id)
                            + '" aria-label="Remove">✕</button>'
                            + '<div class="eb-attach-icon">⚠️</div>'
                            + '<div class="eb-attach-name">' + _ebEsc((a && a.name) || 'attachment') + '</div>'
                            + '</div>';
                    }
                }).join('');

                // Delegated: the grid is re-rendered constantly, so binding
                // per item would mean rebinding every render (and leaking
                // whatever the DOM keeps a reference to).
                attachGrid.querySelectorAll('.eb-attach-remove').forEach(function (btn) {
                    btn.onclick = function () { removeAttachment(btn.dataset.id); };
                });
                attachGrid.querySelectorAll('.eb-attach-retry').forEach(function (btn) {
                    btn.onclick = function () {
                        var a = attachments.filter(function (x) { return x.id === btn.dataset.retry; })[0];
                        if (!a) return;
                        a._previewError = null;
                        a._previewPending = false;
                        refreshAttachmentPreview(a);
                    };
                });
            }

            // ═══════════════════════════════════════════════════════
            // EMBEDS (accordion) — array ownership + max/delete rules
            // stay here; the card markup lives in the shared composer
            // ═══════════════════════════════════════════════════════
            function duplicateEmbed(i) {
                if (state.embeds.length >= LIMITS.embedsMax) {
                    toast('Max ' + LIMITS.embedsMax + ' embeds per message', 'warning');
                    return;
                }
                state.embeds.splice(i + 1, 0, JSON.parse(JSON.stringify(state.embeds[i])));
                activeEmbed = i + 1;
                dirty = true;
                editor.render(); renderPreview(); saveDraft(); pushHistory();
            }
            function deleteEmbed(i) {
                if (state.embeds.length <= 1) {
                    toast('At least one embed is required — clear its fields instead', 'warning');
                    return;
                }
                state.embeds.splice(i, 1);
                activeEmbed = Math.min(activeEmbed, state.embeds.length - 1);
                dirty = true;
                editor.render(); renderPreview(); saveDraft(); pushHistory();
            }

            var editor = EC.mountEditor({
                listEl: embedsList,
                counterEl: embedCounter,
                maxEmbeds: LIMITS.embedsMax,
                extraFields: EXTRA_FIELDS,
                getEmbeds: function () { return state.embeds; },
                getActive: function () { return activeEmbed; },
                setActive: function (i) { activeEmbed = i; },
                onRender: function () { ctx.counter('editorRenders'); },
                onChange: function () { dirty = true; renderPreview(); saveDraft(); pushHistory(); },
                onDuplicate: duplicateEmbed,
                onDelete: deleteEmbed,
            });

            ctx.on(need('eb-add-embed'), 'click', function () {
                if (state.embeds.length >= LIMITS.embedsMax) {
                    toast('Max ' + LIMITS.embedsMax + ' embeds per message', 'warning');
                    return;
                }
                state.embeds.push(blankEmbed());
                activeEmbed = state.embeds.length - 1;
                dirty = true;
                editor.render();
                renderPreview();
                saveDraft();
                pushHistory();
            });

            // ═══════════════════════════════════════════════════════
            // LIVE PREVIEW
            // ═══════════════════════════════════════════════════════
            function resolveUserAsync(id) {
                if (userNameCache[id] || userResolving[id]) return;
                userResolving[id] = true;
                ctx.fetch('/api/guild/resolve-user/' + id).then(function (r) { return r.json(); })
                    .then(function (data) {
                        delete userResolving[id];
                        if (data.resolved) userNameCache[id] = data.username;
                        if (!ctx.isDestroyed()) renderPreview();
                    }, function () { delete userResolving[id]; });
            }

            function renderPreview() {
                if (ctx.isDestroyed()) return;
                ctx.counter('previewUpdates');
                EC.renderPreview(previewBox, {
                    content: state.content,
                    embeds: state.embeds,
                    attachments: attachments,
                    botIdentity: botIdentity,
                    emptyText: 'Start typing to see a preview...',
                    attachmentPreviewUrl: getAttachmentPreviewUrl,
                    lookups: {
                        roles: roleMap,
                        channels: channelMap,
                        users: userNameCache,
                        onUserResolve: resolveUserAsync,
                    },
                });
            }

            // ═══════════════════════════════════════════════════════
            // SEND
            // ═══════════════════════════════════════════════════════
            function cleanEmbedsForPayload() { return EC.cleanEmbedsForPayload(state.embeds); }

            function preflightErrors(channelId, embeds) {
                // Client-side mirror of the server's rules for the fields
                // that fail SILENTLY if they are wrong (Discord needs an
                // author name with an icon, footer text with a footer icon).
                // The server re-checks everything — this exists to save the
                // user a 25MB upload, not to be trusted.
                var errors = [];
                if (!channelId) errors.push('Pick a channel to send to.');
                if (state.content.length > LIMITS.contentMax) {
                    errors.push('Message content is ' + state.content.length
                        + ' characters; Discord\'s limit is ' + LIMITS.contentMax + '.');
                }
                if (!state.content && !embeds.length && !attachments.length) {
                    errors.push('Nothing to send — add content, an embed, or an attachment.');
                }
                embeds.forEach(function (e, i) {
                    var n = i + 1;
                    if (e.author && e.author.icon_url && !e.author.name) {
                        errors.push('Embed ' + n + ': an author icon needs an author name.');
                    }
                    if (e.author && e.author.url && !e.author.name) {
                        errors.push('Embed ' + n + ': an author link needs an author name.');
                    }
                    if (e.footer && e.footer.icon_url && !e.footer.text) {
                        errors.push('Embed ' + n + ': a footer icon needs footer text.');
                    }
                    if (e.title && e.title.length > LIMITS.embedTitleMax) {
                        errors.push('Embed ' + n + ' title is ' + e.title.length + ' characters; limit '
                            + LIMITS.embedTitleMax + '.');
                    }
                    if (e.description && e.description.length > LIMITS.embedDescriptionMax) {
                        errors.push('Embed ' + n + ' description is ' + e.description.length + ' characters; limit '
                            + LIMITS.embedDescriptionMax + '.');
                    }
                });
                return errors;
            }

            ctx.on(sendBtn, 'click', function () {
                var channelId = pickerValue('#eb-send-channel');
                var embeds = cleanEmbedsForPayload();
                var problems = preflightErrors(channelId, embeds);
                if (problems.length) {
                    setStatus(problems[0], false);
                    toast(problems[0], 'warning');
                    return;
                }

                confirmThen('Send this message to the selected channel now? This cannot be undone.',
                    function () {
                        if (window.setLoading) window.setLoading(sendBtn, true, 'Sending…');
                        var fd = new FormData();
                        fd.append('channel_id', channelId);
                        fd.append('payload_json', JSON.stringify({
                            content: state.content || undefined,
                            embeds: embeds,
                        }));
                        attachments.forEach(function (a) {
                            if (a.source !== 'remote') fd.append('files', a.blob, a.name);
                        });

                        ctx.fetch('/api/embedbuilder/send', {
                            method: 'POST',
                            headers: { 'X-CSRF-Token': window.__CSRF_TOKEN__ || '' },
                            body: fd,
                        }).then(function (res) { return res.json(); }).then(function (data) {
                            if (ctx.isDestroyed()) return;
                            if (data.success) {
                                setStatus('Sent!', true);
                                toast('Message sent!');
                                (data.warnings || []).forEach(function (w) { toast(w, 'warning'); });
                                // Attachments are one-shot: the blobs were
                                // consumed by the upload, so the list, the
                                // preview and the draft must all agree that
                                // there is nothing left.
                                revokeAllAttachmentPreviews();
                                attachments = [];
                                renderAttachments();
                                renderPreview();
                                saveDraft();
                            } else {
                                // Field-path errors from the server (Discord's
                                // own 400 shape) — show the first, log the rest.
                                setStatus('❌ ' + (data.error || 'Send failed'), false);
                                toast(data.error || 'Send failed', 'error');
                                if (data.errors && data.errors.length > 1) {
                                    ctx.debug('send-validation-errors', data.errors);
                                }
                            }
                        }, function () {
                            if (!ctx.isDestroyed()) {
                                setStatus('❌ Connection error', false);
                                toast('Connection error', 'error');
                            }
                        }).then(function () {
                            if (window.setLoading) window.setLoading(sendBtn, false);
                        });
                    });
            });

            // ═══════════════════════════════════════════════════════
            // SAVED TEMPLATES (this page's own "Saved Embed" precursor)
            // ═══════════════════════════════════════════════════════
            function loadTemplateOptions() {
                return ctx.fetchJSON('/api/embedbuilder/templates').then(function (data) {
                    if (ctx.isDestroyed()) return;
                    templateSelect.innerHTML = '<option value="">Select a template…</option>'
                        + (data.templates || []).map(function (t) {
                            return '<option value="' + _ebAttr(t) + '">' + _ebEsc(t) + '</option>';
                        }).join('');
                }, function () {
                    ctx.counter('templateListFailures');
                    if (!ctx.isDestroyed()) {
                        templateSelect.innerHTML = '<option value="">Could not load templates</option>';
                    }
                });
            }

            ctx.on(need('eb-save-template-btn'), 'click', function () {
                var name = need('eb-template-name').value.trim();
                if (!name) { toast('Enter a template name', 'warning'); return; }
                var btn = need('eb-save-template-btn');
                if (window.setLoading) window.setLoading(btn, true);
                ctx.fetch('/api/embedbuilder/template/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: name, content: state.content, embeds: cleanEmbedsForPayload() }),
                }).then(function (res) { return res.json(); }).then(function (data) {
                    if (ctx.isDestroyed()) return;
                    if (data.success) { toast('Template saved!'); loadTemplateOptions(); }
                    else toast(data.error || 'Error saving template', 'error');
                }, function () {
                    if (!ctx.isDestroyed()) toast('Connection error', 'error');
                }).then(function () {
                    if (window.setLoading) window.setLoading(btn, false);
                });
            });

            ctx.on(need('eb-copy-json-btn'), 'click', function () {
                var payload = { content: state.content, embeds: cleanEmbedsForPayload() };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
                    toast('JSON copied!');
                } else {
                    toast('Clipboard unavailable in this browser', 'warning');
                }
            });

            ctx.on(need('eb-load-template-btn'), 'click', function () {
                var name = templateSelect.value;
                if (!name) { toast('Pick a template to load', 'warning'); return; }
                ctx.fetchJSON('/api/embedbuilder/template/' + encodeURIComponent(name)).then(function (data) {
                    if (ctx.isDestroyed()) return;
                    if (!data.template) { toast('Template not found', 'error'); return; }
                    // Copy-on-load: the template is parsed into fresh objects,
                    // never referenced — editing here must not touch what is
                    // stored until the user saves again.
                    state.content = data.template.content || '';
                    state.embeds = EC.embedsFromApi(data.template.embeds);
                    activeEmbed = 0;
                    dirty = true;
                    renderAll();
                    saveDraft();
                    pushHistory();
                    toast('Template loaded!');
                }, function () {
                    if (!ctx.isDestroyed()) toast('Could not load that template', 'error');
                });
            });

            ctx.on(need('eb-delete-template-btn'), 'click', function () {
                var name = templateSelect.value;
                if (!name) { toast('Pick a template to delete', 'warning'); return; }
                confirmThen('Delete template "' + name + '"?', function () {
                    ctx.fetch('/api/embedbuilder/template/' + encodeURIComponent(name), { method: 'DELETE' })
                        .then(function (res) { return res.json(); }).then(function (data) {
                            if (ctx.isDestroyed()) return;
                            if (data.success) { toast('Template deleted', 'info'); loadTemplateOptions(); }
                            else toast('Error deleting template', 'error');
                        }, function () {
                            if (!ctx.isDestroyed()) toast('Connection error', 'error');
                        });
                });
            });

            ctx.on(need('eb-clear-btn'), 'click', function () {
                confirmThen('Clear the entire composer — content, all embeds, and attachments? '
                    + 'This cannot be undone.', function () {
                    revokeAllAttachmentPreviews();
                    state = { content: '', embeds: [blankEmbed()] };
                    attachments = [];
                    activeEmbed = 0;
                    history = [];
                    historyIndex = -1;
                    dirty = true;
                    renderAll();
                    idbClear();
                    pushHistory();
                    toast('Composer cleared', 'info');
                });
            });

            ctx.on(undoBtn, 'click', undo);
            ctx.on(redoBtn, 'click', redo);
            // Bound to the page, not the document forever: the old code left
            // this listening on every other dashboard page after a visit.
            ctx.on(document, 'keydown', function (e) {
                if (!(e.ctrlKey || e.metaKey)) return;
                var key = String(e.key || '').toLowerCase();
                var inField = e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName || '');
                if (key === 'z' && !e.shiftKey && !inField) { e.preventDefault(); undo(); }
                else if ((key === 'y' || (key === 'z' && e.shiftKey)) && !inField) {
                    e.preventDefault(); redo();
                }
            });

            // ═══════════════════════════════════════════════════════
            // RENDER ALL  (synchronous, no I/O)
            // ═══════════════════════════════════════════════════════
            function renderAll() {
                contentInput.value = state.content;
                updateContentCounter();
                editor.render();
                renderAttachments();
                renderPreview();
            }

            // ═══════════════════════════════════════════════════════
            // FIRST PAINT — nothing here waits for anything
            // ═══════════════════════════════════════════════════════
            renderAll();
            ctx.mark('firstPaint');
            ctx.counters.firstPaintMs = Math.round(ctx.marks.firstPaint - ctx.marks.initStart);
            history = [snapshotForHistory()];
            historyIndex = 0;
            updateUndoRedoButtons();

            // ── After paint: everything that touches the network or
            //    IndexedDB, in parallel, each independently optional ──
            function afterPaint(fn) {
                if (typeof requestAnimationFrame === 'function') {
                    requestAnimationFrame(function () { ctx.timeout(fn, 0); });
                } else {
                    ctx.timeout(fn, 0);
                }
            }

            function loadLimits() {
                var t0 = now();
                return ctx.fetchJSON('/api/embedbuilder/limits').then(function (data) {
                    if (ctx.isDestroyed()) return;
                    LIMITS = limitsFrom(data && data.limits);
                    ctx.counters.limitsMs = Math.round(now() - t0);
                    // Re-render only what the numbers affect (and keep the
                    // browser's own input cap in step with the table).
                    if (contentInput.getAttribute('maxlength') !== String(LIMITS.contentMax)) {
                        contentInput.setAttribute('maxlength', String(LIMITS.contentMax));
                    }
                    updateContentCounter();
                    renderAttachments();
                }, function () {
                    ctx.counter('limitsFailures');
                    ctx.counters.limitsMs = Math.round(now() - t0);
                });
            }

            function loadMentionLookups() {
                var t0 = now();
                return Promise.all([
                    ctx.fetchJSON('/api/guild/roles'),
                    ctx.fetchJSON('/api/guild/channels'),
                ]).then(function (both) {
                    if (ctx.isDestroyed()) return;
                    roleMap = {};
                    (both[0].results || []).forEach(function (r) { roleMap[r.id] = { name: r.text, color: r.color }; });
                    channelMap = {};
                    (both[1].results || []).forEach(function (c) { channelMap[c.id] = { name: c.text }; });
                    ctx.counters.lookupsMs = Math.round(now() - t0);
                    ctx.counter('lookupsLoaded');
                    renderPreview();       // mentions resolve from IDs to names
                }, function () {
                    // Unresolved mentions render as raw IDs, exactly like
                    // Discord does before it resolves them — not an error state.
                    ctx.counter('lookupFailures');
                    ctx.counters.lookupsMs = Math.round(now() - t0);
                });
            }

            function refreshBotIdentity() {
                // The page already rendered with the server-injected
                // identity, so this is a refinement, never a prerequisite.
                var t0 = now();
                return ctx.fetchJSON('/api/botprofile/config').then(function (data) {
                    if (ctx.isDestroyed()) return;
                    var live = data.live || {}, stored = data.stored || {};
                    var name = stored.nickname || live.nick || live.username;
                    var avatar = live.guild_avatar_url || stored.avatar_url || live.global_avatar_url;
                    if (!name && !avatar) return;
                    if (name === botIdentity.name && avatar === botIdentity.avatar) return;
                    botIdentity = { name: name || botIdentity.name, avatar: avatar || null, source: 'live' };
                    ctx.counters.identityMs = Math.round(now() - t0);
                    renderPreview();
                }, function () {
                    ctx.counter('identityFailures');
                });
            }

            function restoreDraftAsync() {
                if (idbState === 'unavailable') return;
                var t0 = now();
                var restore = idbGet('composer').then(function (draft) {
                    ctx.counters.idbRestoreMs = Math.round(now() - t0);
                    if (!draft || ctx.isDestroyed()) return;
                    if (dirty) {
                        // The user started typing before the draft arrived
                        // (only possible if storage is pathologically slow).
                        // Applying it now would overwrite their typing, so it
                        // is reported instead of silently discarded.
                        ctx.counter('draftSkippedDirty');
                        ctx.debug('draft-skipped-dirty', 'dirty');
                        return;
                    }
                    if (draft.content) state.content = draft.content;
                    if (draft.embeds && draft.embeds.length) state.embeds = draft.embeds;
                    if (draft.attachments && draft.attachments.length) {
                        attachments = draft.attachments.map(function (a) {
                            var rec = Object.assign({}, a, { source: a.source || 'local' });
                            // Transient preview state never survives a
                            // reload: a blob: URL dies with the tab, and a
                            // stale "pending" flag would pin the tile on
                            // "preview…" forever.
                            rec._previewUrl = null;
                            rec._dataUrl = null;
                            rec._previewPending = false;
                            rec._previewError = null;
                            rec._previewRefreshing = false;
                            rec._previewPromise = null;
                            return rec;
                        });
                    }
                    activeEmbed = Math.min(activeEmbed, state.embeds.length - 1);
                    ctx.counter('draftRestored');
                    renderAll();
                    history = [snapshotForHistory()];
                    historyIndex = 0;
                    updateUndoRedoButtons();
                });
                var timeout = new Promise(function (resolve) {
                    ctx.timeout(function () { resolve('timeout'); }, DRAFT_RESTORE_TIMEOUT_MS);
                });
                return Promise.race([restore, timeout]).then(function (result) {
                    if (result === 'timeout') {
                        ctx.counter('draftRestoreTimeout');
                        // The composer is already interactive; this only
                        // means the stored draft was not read in time.
                        markIdbUnavailable('restore-timeout',
                            'Draft storage is responding too slowly, so autosave is off for this '
                            + 'session. The composer works normally; a refresh will not restore it.');
                    }
                });
            }

            afterPaint(function () {
                ctx.mark('postPaint');
                ctx.counters.postPaintMs = Math.round(ctx.marks.postPaint - ctx.marks.initStart);
                // Four independent, failure-tolerant calls. None of them
                // blocks paint, and each re-renders only what it owns.
                loadLimits();
                loadTemplateOptions();
                loadMentionLookups();
                refreshBotIdentity();
                restoreDraftAsync();
                if (ctx.debug) ctx.debug('first-paint', NERO.debug.report());
            });
        },

        // The registry already removes listeners/timers/object URLs and
        // aborts in-flight requests through `ctx`; this is for page state
        // that outlives the DOM (it holds none today, but the hook is what
        // makes that statement checkable).
        destroy: function (root, ctx) {
            ctx.debug('destroy', 'embed-builder');
        },
    });
})();
