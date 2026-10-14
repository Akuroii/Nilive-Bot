/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — the Discord markdown renderer.

   Phase 1, step 2. Pure. No DOM, no timers, no global mutable state,
   no storage, no network. Same input → same output, always.

   WHY A SECOND RENDERER EXISTS AT ALL
   -----------------------------------
   `embed-composer.js` already renders Discord markdown, and v1 keeps
   using it — untouched. This module is the v2 implementation, and its
   behaviour is *proven equal* to v1's by `scripts/test_discord_markdown.js`,
   which treats the live v1 module as the oracle over a committed
   corpus (`scripts/fixtures/markdown_corpus.json`) plus a recorded
   golden snapshot (`scripts/fixtures/markdown_golden.json`).

   Duplication here is deliberate and temporary: extracting the shared
   implementation would have meant touching the frozen v1 module, and
   the boundary is only provable with two independent implementations
   and a corpus between them. The golden file records the hash of the
   v1 source it was generated from, so the day v1 *is* changed (the
   phase that deletes the legacy page) the harness says so out loud
   instead of drifting.

   WHAT THIS ADDS OVER v1
   ----------------------
   CONTEXT. Discord does not render one markdown dialect: a message
   content, an embed description and a field value get full markdown,
   while a title, author name, footer, field name, button label and
   select option are plain text. v1 renders the markup contexts through
   `renderDiscordMarkup` and the literal ones through `esc()` at the
   call site; this module makes that a property of the *context*, so a
   caller cannot get it wrong by forgetting which helper to call.

   EQUALITY IS EXACT — INCLUDING THE AWKWARD PARTS
   -----------------------------------------------
   There is no "equivalent except for…" here. The literal surfaces reproduce
   v1's `esc(value)` byte for byte, control characters included, and the
   markup surfaces reproduce v1's `renderDiscordMarkup` byte for byte,
   including the placeholder-shaped caveats that come with it. Where v1's
   behaviour is imperfect (crossed tags for bold-italic, a fence in the
   middle of a sentence, no backslash escapes) v2 reproduces the imperfection
   and the corpus records it as a known defect: this step establishes the
   compatibility boundary, and correcting Discord fidelity is a separate,
   deliberate change — not something that happens by accident inside a
   compatibility pass.

   WHAT IT DELIBERATELY DOES NOT DO
   --------------------------------
   No new markdown features. Discord's real set includes spoilers,
   blockquotes, headers, subtext, lists, masked links and timestamps —
   v1 implements none of them, so neither does this, and the corpus
   records each one as a known gap with the literal output both
   implementations currently produce. Turning them on later is a
   deliberate, visible change to this file plus the corpus.

   OPEN QUESTION (recorded, not decided here)
   ------------------------------------------
   `fieldName` is treated as fully literal, matching v1 (`esc(f.name)`
   in the preview) and the review's per-surface matrix. Discord may
   resolve custom emoji and mentions inside field names; the context
   table can express that (`tokens: true, markdown: false`) and the
   corpus has the case pinned either way, so changing it is one line
   and one deliberate test update — not an accident.

   Consumed by: the v2 preview (step 3). NOT consumed by v1.
   Tested by: scripts/test_discord_markdown.js.
   ═══════════════════════════════════════════════════════════════ */
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    // ── Contexts ──────────────────────────────────────────────────
    // markdown: run the inline markdown pass
    // tokens:   resolve <@id>/<#id>/<@&id>/<:name:id> into spans/images
    // emojiOnly: the message-content "-only" sizing flag
    // label:    the surface name, for docs/tests
    const CONTEXTS = {
        content: { label: 'message content', markdown: true, tokens: true, emojiOnly: true },
        description: { label: 'embed description', markdown: true, tokens: true, emojiOnly: false },
        fieldValue: { label: 'embed field value', markdown: true, tokens: true, emojiOnly: false },
        fieldName: { label: 'embed field name', markdown: false, tokens: false, emojiOnly: false },
        title: { label: 'embed title', markdown: false, tokens: false, emojiOnly: false },
        author: { label: 'embed author name', markdown: false, tokens: false, emojiOnly: false },
        footer: { label: 'embed footer text', markdown: false, tokens: false, emojiOnly: false },
        buttonLabel: { label: 'component label', markdown: false, tokens: false, emojiOnly: false },
        selectOption: { label: 'select option text', markdown: false, tokens: false, emojiOnly: false },
    };

    const DEFAULT_CONTEXT = 'content';

    // C0 controls that can never render. Also the placeholder alphabet used
    // below, which is why they are removed from the source text and scrubbed
    // from the output as a backstop.
    const CONTROL_CHARS_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g;
    const EMOJI_UNICODE_RE = /\p{Extended_Pictographic}(\u200d\p{Extended_Pictographic})*\ufe0f?/gu;

    // Token shapes Discord sends: custom emoji (animated flag optional),
    // channel, role, user (the `!` form is the old nickname alias).
    const TOKEN_PARTS = '<(a?):(\\w+):(\\d+)>|<#(\\d+)>|<@&(\\d+)>|<@!?(\\d+)>';
    // Built per call: v1's module-level /g regex needs `lastIndex = 0`
    // bookkeeping before every `exec`, which is exactly the kind of shared
    // mutable state this module must not have.
    const tokenRe = () => new RegExp(TOKEN_PARTS, 'g');
    const tokenReOne = () => new RegExp(TOKEN_PARTS);
    const tokenPlaceholderRe = () => /\u0001(\d{4})\u0002/g;
    const codePlaceholderRe = () => /\u0003(\d{4})\u0004/g;

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }
    function attr(s) { return esc(s); }

    function stripControls(text) {
        return String(text == null ? '' : text).replace(CONTROL_CHARS_RE, '');
    }

    function emojiUrl(id, animated) {
        return `https://cdn.discordapp.com/emojis/${id}.${animated ? 'gif' : 'png'}`;
    }

    /**
     * One token → HTML. Identical to v1's `renderToken`, including the
     * deliberate quirks kept for byte-equality: the emoji `alt` is
     * unterminated there, and a role's colour is injected as an inline
     * style. Those are recorded in the harness as inherited behaviour.
     */
    function renderToken(match, lookups) {
        const m = tokenReOne().exec(match);
        if (!m) return esc(match);
        const animFlag = m[1], ename = m[2], eid = m[3];
        const chid = m[4], rid = m[5], uid = m[6];
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
    // One left-to-right scan, exactly as v1 does it (and for the reasons
    // recorded there): a scan cannot mistake its own placeholder output for
    // input, a run of backticks closes at the next run of the same length,
    // a fence may close on a longer run, and an unclosed run stays as typed.
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
            if (closeAt === -1) {
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

    // A fence's opening line is its language: dropped, with the newline after
    // it and a trailing one. Only when the fence opens a line.
    function fencedCodeBody(body) {
        const firstLine = body.match(/^[^\n]*\n/);
        if (firstLine && /^[ \t]*[A-Za-z0-9_+#.-]*[ \t]*\r?\n$/.test(firstLine[0])) {
            body = body.slice(firstLine[0].length);
        }
        return body.replace(/^\r?\n/, '').replace(/\r?\n[ \t]*$/, '');
    }

    // CommonMark: content that starts AND ends with a space (and is not all
    // spaces) loses one space at each end.
    function inlineCodeBody(body) {
        return (/^[ \t]/.test(body) && /[ \t]$/.test(body) && body.trim())
            ? body.slice(1, -1) : body;
    }

    function resolveContext(context) {
        if (context === undefined || context === null) return CONTEXTS[DEFAULT_CONTEXT];
        const found = CONTEXTS[context];
        if (!found) throw new TypeError('unknown markdown context: ' + context);
        return found;
    }

    /**
     * Render text for one surface.
     *
     * @param {string} text
     * @param {{context?: string, lookups?: object, checkEmojiOnly?: boolean}} [opts]
     * @returns {{html: string, isEmojiOnly: boolean, context: string}}
     */
    function render(text, opts) {
        opts = opts || {};
        const contextName = opts.context === undefined || opts.context === null
            ? DEFAULT_CONTEXT : opts.context;
        const context = resolveContext(contextName);
        const lookups = opts.lookups || {};

        if (!text) return { html: '', isEmojiOnly: false, context: contextName };

        // Plain-text surfaces end here, with EXACTLY what v1's preview does:
        // `esc(value)` on the raw value at the call site. No markdown, no
        // tokens — and, importantly, no control-character stripping either.
        // v1's esc() passes C0 controls through on these surfaces and the
        // compatibility contract is byte equality, so they pass through here
        // too; adding a scrub would be a behaviour change dressed up as
        // consistency. (The markup path is different because v1's own
        // renderDiscordMarkup strips controls — see below.)
        if (!context.markdown && !context.tokens) {
            return { html: esc(text), isEmojiOnly: false, context: contextName };
        }

        // Markup surfaces follow v1's renderDiscordMarkup step for step.
        // Controls first: they cannot render, and one shaped like this
        // module's own placeholder would otherwise masquerade as one.
        text = stripControls(text);
        if (!text) return { html: '', isEmojiOnly: false, context: contextName };

        const tokens = [];
        let working = text;
        if (context.tokens) {
            working = text.replace(tokenRe(), (match) => {
                tokens.push(match);
                return `\u0001${String(tokens.length - 1).padStart(4, '0')}\u0002`;
            });
        }

        // Emoji-only sizing: once every token/emoji is stripped, whitespace is
        // all that may remain, and at least one emoji/token must have existed.
        let isEmojiOnly = false;
        if (context.emojiOnly && opts.checkEmojiOnly) {
            const stripped = working.replace(/\u0001\d+\u0002/g, '').replace(EMOJI_UNICODE_RE, '').trim();
            isEmojiOnly = stripped.length === 0 &&
                (tokens.length + (text.match(EMOJI_UNICODE_RE) || []).length) > 0;
        }

        let escaped = esc(working);

        // Code out of the way before the markdown pass: Discord does not
        // interpret markdown inside code. Fixed-width keys keep the two
        // placeholder alphabets from ever sharing an index.
        const codeChunks = [];
        const stashCode = (html) => {
            codeChunks.push(html);
            return `\u0003${String(codeChunks.length - 1).padStart(4, '0')}\u0004`;
        };
        escaped = scanCode(escaped, (body, isFence) => isFence
            ? stashCode(`<pre class="eb-code-block"><code>${fencedCodeBody(body)}</code></pre>`)
            : stashCode(`<code class="eb-code">${inlineCodeBody(body)}</code>`));

        // Markdown — bold before italic so `**x**` is not half-consumed.
        if (context.markdown) {
            escaped = escaped.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
            escaped = escaped.replace(/__([\s\S]+?)__/g, '<u>$1</u>');
            escaped = escaped.replace(/\*([\s\S]+?)\*/g, '<em>$1</em>');
            escaped = escaped.replace(/~~([\s\S]+?)~~/g, '<s>$1</s>');
        }

        if (isEmojiOnly) {
            escaped = escaped.replace(EMOJI_UNICODE_RE, (m) => `<span>${m}</span>`);
        }

        // Code back first, then tokens (their URLs are generated here, never
        // user-supplied, so they must not be stashed as code).
        escaped = escaped.replace(codePlaceholderRe(), (m, idx) => {
            const chunk = codeChunks[parseInt(idx, 10)];
            return chunk === undefined ? m : chunk;
        });
        escaped = escaped.replace(tokenPlaceholderRe(), (m, idx) => {
            const token = tokens[parseInt(idx, 10)];
            return token === undefined ? m : renderToken(token, lookups);
        });

        // Backstop: nothing of the placeholder alphabet reaches the output.
        escaped = escaped.replace(CONTROL_CHARS_RE, '');

        return { html: escaped, isEmojiOnly, context: contextName };
    }

    /**
     * Convenience for the plain-text surfaces: byte-identical to v1's
     * `esc(value)` at the call site. Deliberately does NOT strip control
     * characters — see the literal branch in `render()`.
     */
    function renderLiteral(text) {
        return esc(text);
    }

    NERO.embed.discordMarkdown = {
        CONTEXTS,
        DEFAULT_CONTEXT,
        render,
        renderLiteral,
        resolveContext,
        esc,
        attr,
        stripControls,
        renderToken,
        emojiUrl,
        CONTROL_CHARS_RE_SOURCE: CONTROL_CHARS_RE.source,
    };
})(window.NERO);
