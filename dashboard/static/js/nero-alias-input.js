/* ProBot-style alias chips.
 *
 * One box per command: type a word, press Space (or Enter / comma) and it
 * becomes a chip with its own × . Space is the commit key because that is what
 * people instinctively press after typing a word; Enter works too, and Backspace
 * on an empty box pops the last chip.
 *
 * Deliberate choices, in case someone is tempted to "improve" it:
 *   - No minimum length. A single character is a valid alias (`k` for /kick)
 *     and is the most common thing admins ask for. Collision handling is the
 *     router's job (utils/message_router.py), not this input's.
 *   - Conflicts are NOT blocked. Saving `k` when a trigger also uses `k` is
 *     allowed; the runtime precedence decides who answers, and this field only
 *     shows a ⚠ on the chip plus the warnings the server returned.
 *   - The real <input type="hidden"> keeps holding the comma-joined string, so
 *     the existing save path works unchanged even with JS disabled.
 */
window.NeroAlias = (function () {
    const MAX_LEN = 32;
    // Letters of any script (this server's commands can be Arabic or Hebrew),
    // digits, - and _. Deliberately a rule about *characters*, not about
    // length or script: a single character is a valid alias, and the server
    // applies the same shape (alias_format_error in dashboard/app.py).
    const VALID = /[\p{L}\p{N}_-]+/u;
    const BAD_CHARS = /[^\p{L}\p{N}_-]/u;
    let _registry = null;        // {aliases: {word: {command, guild_id, scope}}}
    let _registryLoading = null;

    function registry() {
        if (_registry) return Promise.resolve(_registry);
        if (!_registryLoading) {
            _registryLoading = fetch('/api/commands/alias-registry')
                .then(r => r.ok ? r.json() : null)
                .then(data => {
                    _registry = data && data.aliases ? data : { aliases: {}, prefix_commands: [] };
                    _registryLoading = null;
                    return _registry;
                })
                .catch(() => { _registryLoading = null; return { aliases: {}, prefix_commands: [] }; });
        }
        return _registryLoading;
    }

    function normalize(raw) {
        return String(raw || '').trim().toLowerCase().replace(/^[!/]+/, '');
    }

    function validate(word) {
        if (!word) return 'empty';
        if (/\s/.test(word)) {
            // separate message, same as the server's: "my alias" is usually
            // someone pasting a phrase, not typing punctuation
            return 'can\u2019t contain spaces — one word per alias';
        }
        if (word.length > MAX_LEN) return 'too long';
        if (!VALID.test(word) || BAD_CHARS.test(word)) {
            return 'only letters, numbers, - and _ are allowed';
        }
        return null;
    }

    function initOne(host) {
        if (host.dataset.naInit === '1') return;
        host.dataset.naInit = '1';

        const source = host.querySelector('input[name]') || host;
        const initial = (source.value || '').split(',').map(normalize).filter(Boolean);

        host.innerHTML = '';
        host.classList.add('na-box');
        if (source !== host) {
            // Re-attach the hidden input we just cleared away. It has to stay
            // *inside* the host and in the document: the save path looks it up
            // by id (`edit-aliases-<cmd>`), so leaving it detached made every
            // save post no aliases at all.
            host.appendChild(source);
            source.classList.add('na-source');
        }

        const chips = document.createElement('div');
        chips.className = 'na-chips';
        const text = document.createElement('input');
        text.type = 'text';
        text.className = 'na-input';
        text.autocomplete = 'off';
        text.spellcheck = false;
        text.placeholder = initial.length ? '' : 'type an alias, then Space…';
        const hint = document.createElement('div');
        hint.className = 'na-hint';
        host.appendChild(chips);
        host.appendChild(text);
        host.appendChild(hint);

        // keep the hidden input authoritative, whatever we render
        const values = [];

        function sync() {
            source.value = values.join(',');
            host.dispatchEvent(new CustomEvent('nero-alias:change', {
                bubbles: true, detail: { values: values.slice() },
            }));
        }

        function setHint(msg, kind) {
            hint.textContent = msg || '';
            hint.className = 'na-hint' + (msg ? ' na-hint--' + (kind || 'error') : '');
        }

        function markConflict(chip, word) {
            registry().then(reg => {
                const hit = (reg.aliases || {})[word];
                if (!hit) return;
                if (hit.command === host.dataset.aliasCommand &&
                    hit.scope !== 'global') {
                    return;                       // this command already owns it
                }
                const owner = hit.command ? '/' + hit.command : 'another command';
                chip.classList.add('na-chip--warn');
                chip.title = `⚠ ${owner} also claims “${word}”` +
                    (hit.scope === 'global'
                        ? ' (stored on the global guild-0 row, which Nero does '
                          + 'not apply to real servers)'
                        : '') +
                    ' — the most recently saved row wins, so saving moves it here.';
                const mark = document.createElement('span');
                mark.className = 'na-chip-warn';
                mark.textContent = '!';
                chip.insertBefore(mark, chip.firstChild);
            });
        }

        function reEdit(word, chip) {
            // Put the word back in the box to be fixed instead of silently
            // deleting the chip: clicking a chip reads as "edit this".
            const i = values.indexOf(word);
            if (i >= 0) values.splice(i, 1);
            render();
            sync();
            text.value = word;
            text.focus();
            setHint(`Editing “${word}” — Space puts it back, × drops it.`, 'ok');
        }

        function render() {
            chips.innerHTML = '';
            values.forEach(word => {
                const chip = document.createElement('span');
                chip.className = 'na-chip';
                const label = document.createElement('span');
                label.className = 'na-chip-text';
                label.textContent = word;
                const x = document.createElement('button');
                x.type = 'button';
                x.className = 'na-chip-x';
                x.setAttribute('aria-label', 'Remove alias ' + word);
                x.textContent = '\u00d7';
                x.addEventListener('click', () => remove(word));
                chip.appendChild(label);
                chip.appendChild(x);
                chip.addEventListener('click', e => {
                    if (e.target === x) return;
                    reEdit(word, chip);
                });
                chips.appendChild(chip);
                markConflict(chip, word);
            });
            text.placeholder = values.length ? '' : 'type an alias, then Space…';
        }

        function add(raw, silent) {
            let word = normalize(raw);
            let problem = validate(word);
            let truncated = false;
            if (problem === 'too long') {
                // Truncate rather than refuse. The cap exists because Discord
                // words have to stay short; a paste that runs a few characters
                // over is far more common than a mistake worth discarding.
                word = word.slice(0, MAX_LEN);
                problem = validate(word);
                truncated = true;
            }
            if (problem) {
                if (!silent) {
                    setHint(word ? `“${word}” — ${problem}` : 'Type something first.');
                    // Leave the text in the box and selected so it can be
                    // fixed in place; clearing it would throw the attempt away.
                    text.value = word;
                    text.select();
                }
                return false;
            }
            if (values.includes(word)) {
                // An accidental second Space must not cost the user the chip —
                // remove(word) here used to delete it, which reads as the field
                // eating your work.
                if (!silent) setHint(`“${word}” is already on this command.`);
                text.value = '';
                return false;
            }
            values.push(word);
            text.value = '';
            setHint(truncated
                ? `Long names get cut at ${MAX_LEN} characters — using “${word}”.`
                : '', truncated ? 'ok' : undefined);
            render();
            sync();
            return true;
        }

        function remove(word) {
            const i = values.indexOf(word);
            if (i >= 0) values.splice(i, 1);
            setHint('');
            render();
            sync();
        }

        function commit() {
            if (!text.value.trim()) return false;
            // add() owns what happens to the box: cleared on success, left
            // selected (and editable) on refusal.
            return add(text.value);
        }

        text.addEventListener('keydown', e => {
            if (e.key === ' ' || e.key === 'Enter' || e.key === ',') {
                e.preventDefault();       // Space would otherwise type a space
                commit();
            } else if (e.key === 'Backspace' && !text.value) {
                e.preventDefault();
                if (values.length) remove(values[values.length - 1]);
            } else if (e.key === 'Escape') {
                text.value = '';
                setHint('');
            }
        });
        text.addEventListener('blur', () => { if (text.value.trim()) commit(); });
        text.addEventListener('paste', e => {
            e.preventDefault();
            const pasted = (e.clipboardData || window.clipboardData).getData('text') || '';
            pasted.split(/[\s,;]+/).forEach(w => add(w, true));
            text.value = '';
        });
        host.addEventListener('click', e => { if (e.target === host) text.focus(); });
        host.addEventListener('focusin', () => { if (hint.textContent) setHint(''); });

        // The panel is loaded lazily and re-filled when a command is opened
        // again, so the host exposes an API instead of assuming one init.
        host._neroAlias = {
            setValues(list) {
                values.length = 0;
                (list || []).forEach(w => add(w, true));
                render();
                sync();
            },
            getValues() { return values.slice(); },
        };

        initial.forEach(w => { if (!add(w, true)) { /* drop stale junk quietly */ } });
        render();
        sync();
    }

    function initAll(root) {
        (root || document).querySelectorAll('[data-alias-input]').forEach(initOne);
    }

    // Used by the save path; mirrors NeroSelect.getMultiValues so the two
    // components look the same from the page's point of view.
    function getValues(el) {
        if (!el) return [];
        if (el._neroAlias) return el._neroAlias.getValues();
        const source = el.dataset && el.dataset.naInit === '1'
            ? (el.querySelector('input[name]') || el)
            : el;
        return (source.value || '').split(',').map(s => s.trim()).filter(Boolean);
    }

    function setValues(el, list) {
        if (!el) return;
        if (el._neroAlias) { el._neroAlias.setValues(list); return; }
        const input = el.querySelector && el.querySelector('input[name]');
        if (input) input.value = (list || []).join(',');
    }

    return { initAll, initOne, getValues, setValues, normalize };
})();

document.addEventListener('DOMContentLoaded', () => window.NeroAlias.initAll(document));
