// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed/views/statusbar.js
// Message Builder v2 — the status / action bar (phase 1, step 5a).
//
// WHAT THIS OWNS
//   The status half of #mb2-bar: one pill ("Saved", "Unsaved changes", …), one
//   detail line (revision / draft id / save time / the reason a save failed)
//   and one optional notice line (a sentence the user must see, e.g. "a draft
//   from a newer version was left untouched").
//
// OWNERSHIP — the reason this file has an opinion about where "dirty" comes from
//   Two different facts are displayed here, and they have two different owners:
//
//     * IS THE DOCUMENT UNSAVED?  → the STORE. It is the single source of truth
//       for the document, its history and its dirty flag; the UI reads
//       store.isDirty() and never derives it from anywhere else.
//     * WHAT IS PERSISTENCE DOING? → the DRAFT SESSION. It owns saving/saved/
//       error/blocked/degraded/revision/timestamps. Its own dirty flag is a
//       persistence-boundary question ("does storage hold this document?"), not
//       the document's state, and is deliberately NOT what gets displayed.
//
//   One consequence worth stating: after a write completes while the user kept
//   typing, the store can read "clean" for one tick while a fresh write is
//   already queued. `pending` (a write is scheduled or in flight) is therefore
//   displayed as unsaved work too — the bar never claims "Saved" while an edit
//   is still waiting to be written.
//
// WHAT THIS DOES NOT DO (step 5 scope)
//   No validator, no limits, no counters, no issue list and no truncation — the
//   validation strip belongs to step 6 and this file never writes to it. No
//   actions yet: the buttons land in 5d (an action container is declared, but
//   nothing is rendered into it in 5a). No dialogs (5d).
//
// Consumed by: embed/message-builder-page.js
// Tested by:   scripts/test_message_builder_page.js (behaviour),
//              scripts/test_message_builder_layout.js (markup/CSS contract).
// ═══════════════════════════════════════════════════════════════
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};
window.NERO.embed.views = window.NERO.embed.views || {};

(function (NERO) {
    'use strict';

    // Human text for the persistence reasons the session/adapter can report.
    // A table, not a validation engine: these are storage outcomes, and every
    // one of them is a state the user can be told about in plain words.
    const REASONS = {
        'no-indexeddb': 'this browser has no IndexedDB',
        'open-timeout': 'draft storage did not respond',
        'open-error': 'draft storage refused to open',
        'open-threw': 'draft storage could not be opened',
        'upgrade-failed': 'draft storage could not be prepared',
        'write-timeout': 'the save timed out',
        'write-error': 'the write failed',
        'write-aborted': 'the write was aborted',
        'transaction-failed': 'the write could not start',
        'request-failed': 'the write could not be queued',
        'no-meta-store': 'draft storage has nowhere to keep the last-draft pointer',
        'not-serializable': 'the message contains something that cannot be saved',
        'write-failed': 'the write failed',
        'unavailable': 'draft storage is unavailable',
        'refused': 'the write was refused',
    };

    const GUARDED = {
        future: 'A draft written by a newer version of the dashboard was left untouched',
        corrupt: 'Your saved draft could not be read and was left untouched',
        foreign: 'The saved record is not a Message Builder draft and was left untouched',
        unsupported: 'The saved draft is from an older format with no migration and was left untouched',
    };

    function reasonText(reason) {
        if (!reason) return '';
        return REASONS[reason] || String(reason);
    }

    function defaultFormatTime(ms) {
        if (!ms) return '';
        const when = new Date(ms);
        if (isNaN(when.getTime())) return '';
        return when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    /** Identity facts that are true regardless of what persistence is doing. */
    function identity(session, withTime, formatTime) {
        const parts = [];
        if (session && Number.isInteger(session.revision) && session.revision > 0) {
            parts.push('revision ' + session.revision);
        }
        if (session && session.documentId) parts.push(session.documentId);
        if (withTime && session && session.updatedAt) {
            const time = formatTime(session.updatedAt);
            if (time) parts.push('saved ' + time);
        }
        return parts.join(' · ');
    }

    /**
     * Turn the two facts (store dirty state + session persistence state) into
     * what the bar shows. Pure, so the priority order below is testable on its
     * own: a blocked record outranks everything, a failure outranks saving,
     * saving outranks a queued write, and degraded storage outranks a plain
     * "unsaved" — because in that state nothing is being saved at all.
     */
    function describe(view, formatTime) {
        view = view || {};
        const session = view.session || {};
        const dirty = !!view.dirty;              // store-owned
        const pending = !!view.pending;           // a write is scheduled or in flight
        const fmt = formatTime || defaultFormatTime;

        if (session.blocked) {
            return {
                state: 'blocked',
                tone: 'danger',
                label: 'Draft protected',
                detail: (GUARDED[session.blocked] || 'The saved draft was left untouched') +
                        ' — nothing will be written over it.',
            };
        }
        if (session.lastError) {
            const why = reasonText(session.lastError.reason);
            const extra = session.lastError.reason === 'not-serializable' && session.lastError.message
                ? ' (' + session.lastError.message + ')'
                : '';
            return {
                state: 'error',
                tone: 'danger',
                label: 'Save failed',
                detail: why + extra + ' — your edits are still here in this tab.',
            };
        }
        if (session.saving) {
            return { state: 'saving', tone: 'muted', label: 'Saving…', detail: identity(session, false, fmt) };
        }
        if (session.degraded) {
            return {
                state: 'degraded',
                tone: 'warn',
                label: 'Editing in memory',
                detail: 'draft storage is unavailable (' + reasonText(session.degraded) +
                        ') — your edits stay in this tab and are not being saved',
            };
        }
        if (dirty) {
            return { state: 'dirty', tone: 'warn', label: 'Unsaved changes', detail: identity(session, false, fmt) };
        }
        if (pending) {
            // The store is clean, but the newest edit has not been written yet.
            return { state: 'pending', tone: 'warn', label: 'Unsaved changes', detail: 'the newest edit is not written yet' };
        }
        if (session.writes > 0) {
            return { state: 'saved', tone: 'ok', label: 'Saved', detail: identity(session, true, fmt) };
        }
        return { state: 'clean', tone: 'muted', label: 'No changes yet', detail: identity(session, false, fmt) };
    }

    /**
     * create({ document, status, actions, formatTime })
     *   document   ownerDocument (the bar lives in the page, not in this file)
     *   status     #mb2-bar-status — role="status" aria-live="polite" in the
     *              markup, so the live region exists before anything is written
     *              into it (that is what makes the first announcement reliable)
     *   actions    #mb2-bar-actions — declared here, filled in 5d
     */
    function create(options) {
        options = options || {};
        const doc = options.document;
        if (!doc || typeof doc.createElement !== 'function') {
            throw new TypeError('statusbar.create needs options.document');
        }
        if (!options.status || typeof options.status.appendChild !== 'function') {
            throw new TypeError('statusbar.create needs options.status (the live region element)');
        }
        const formatTime = typeof options.formatTime === 'function' ? options.formatTime : defaultFormatTime;

        const pill = doc.createElement('span');
        pill.className = 'mb2-status-pill';
        const detail = doc.createElement('span');
        detail.className = 'mb2-status-detail';
        const notice = doc.createElement('span');
        notice.className = 'mb2-status-notice';
        notice.hidden = true;

        // Created once per mount; later renders only change text/tone, and only
        // when the value actually changed — writing identical text on every
        // keystroke would be a DOM write per key for no reason.
        const status = options.status;
        status.appendChild(pill);
        status.appendChild(detail);
        status.appendChild(notice);
        if (options.actions) options.actions.className = 'mb2-bar-actions';

        let last = { state: null, label: null, tone: null, detail: null, notice: null };
        let destroyed = false;

        function writeTone(tone) {
            if (tone === last.tone) return;
            if (last.tone) status.classList.remove('mb2-tone-' + last.tone);
            status.classList.add('mb2-tone-' + tone);
            last.tone = tone;
        }

        function render(view) {
            if (destroyed) return last;
            const next = describe(view, formatTime);
            if (next.label !== last.label) { pill.textContent = next.label; last.label = next.label; }
            if (next.detail !== last.detail) { detail.textContent = next.detail; last.detail = next.detail; }
            writeTone(next.tone || 'muted');
            const text = (view && view.notice && view.notice.text) || '';
            if (text !== last.notice) {
                notice.textContent = text;
                notice.hidden = !text;
                last.notice = text;
            }
            if (view && view.notice && view.notice.tone) {
                notice.setAttribute('data-tone', view.notice.tone);
            }
            last.state = next.state;
            return last;
        }

        function destroy() {
            if (destroyed) return false;
            destroyed = true;
            [pill, detail, notice].forEach(function (node) {
                if (status.removeChild) {
                    try { status.removeChild(node); } catch (e) { /* already gone */ }
                }
            });
            return true;
        }

        return {
            render: render,
            destroy: destroy,
            describe: function (view) { return describe(view, formatTime); },
            last: function () { return Object.assign({}, last); },
            // test/debug seam
            reasonText: reasonText,
        };
    }

    NERO.embed.views.statusbar = {
        create: create,
        describe: describe,
        reasonText: reasonText,
        REASONS: REASONS,
        GUARDED: GUARDED,
        defaultFormatTime: defaultFormatTime,
    };
})(window.NERO);
