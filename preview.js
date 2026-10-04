/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — the differential preview engine.

   Phase 1, step 3. The boundary is:

       document  →  model.toDiscordPayload(doc, {withKeys:true})  →  patch

   The preview renders WHAT WILL BE SENT (the canonical payload), never
   the editor's internal shape, so "the preview is the message" is an
   assertion rather than a promise. The only addition on top of the wire
   payload is the `key` field, taken from the ids the Step 1 model
   generated — those keys, and nothing else, give DOM nodes their
   identity across patches. This file never invents a key and never uses
   an array index as one.

   WHY A PATCHER AND NOT innerHTML
   -------------------------------
   v1 rebuilt the whole preview from a string on every keystroke. That is
   correct but it re-created every node, so every image re-decoded and no
   node could hold identity (measured: 40 <img> elements re-created for 20
   keystrokes). This engine keeps the tree and writes only what changed:

     * text        → the text of the owning node, only when it differs
     * markup      → innerHTML of the owning LEAF, only when the memoised
                     render input differs (so unchanged text costs no parse)
     * attributes  → setAttribute/removeAttribute only when the value differs
     * style       → one attribute write on the element that owns the style
     * lists       → keyed reconciliation: reuse, reorder, insert, remove

   The reconciler never touches a node whose key still exists, which is
   what preserves identity for images, fields and sibling embeds.

   DETERMINISM AND PURITY
   ----------------------
   No framework, no timers, no asynchronous work, no randomness, and the
   patch path never reads the wall clock: the time is injected through
   `opts.now()` and only the two time-dependent strings use it. The same
   inputs produce the same DOM bytes, which is what makes the harness able
   to assert `fullRender(d) === mount(d1) then patch(d2)`.

   STATS
   -----
   Every write goes through a counted helper, and `preview.stats()` reports
   what the last patches actually did. That is how "a one-character edit
   costs one text write and nothing else" becomes a measured fact instead
   of an intention — in the test harness and, later, in the dev HUD.

   NOT IN THIS FILE (deliberately)
   -------------------------------
   Attachments (phase 2), component rows (phase 3), actions (phase 4):
   each is one additional region plus one branch, and none of them changes
   the reconciler. The mount passed to `create()` is owned by the preview:
   its children are managed, so a host must not put its own content there.

   Consumed by: the v2 page shell (step 5), the test harness.
   Tested by: scripts/test_preview.js.
   ═══════════════════════════════════════════════════════════════ */
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    const model = NERO.embed.model;
    const markdown = NERO.embed.discordMarkdown;
    if (!model || !markdown) {
        throw new Error('embed/preview.js needs embed/model.js and embed/discord-markdown.js loaded first');
    }

    // Wire defaults and the exact inline styles v1's preview used, so the
    // existing stylesheet keeps working unchanged (no CSS is added here).
    const DEFAULT_ACCENT = '#7c5cbf';
    const AUTHOR_ICON_STYLE = 'width:16px;height:16px;border-radius:3px;object-fit:cover;flex-shrink:0;';
    const FOOTER_ICON_STYLE = 'width:20px;height:20px;border-radius:50%;object-fit:cover;flex-shrink:0;';
    const SEPARATOR_STYLE = 'opacity:.6;';
    const LINK_RESET = 'color:inherit;text-decoration:none;';
    const AUTHOR_LINK_STYLE = LINK_RESET + 'display:flex;align-items:center;gap:6px;';

    const MEDIA_SRC_RE = /^(https?:|blob:|data:)/i;
    const SAFE_HREF_RE = /^https?:\/\//i;
    const ATTACHMENT_RE = /^attachment:\/\//i;

    const EMPTY_TEXT_DEFAULT = 'Start typing to see a preview...';
    const FIELD_INLINE_STYLE = 'width:31%;';
    const FIELD_FULL_STYLE = 'width:100%;';

    // ── Value helpers (mirrors of v1's, kept local so preview.js does not
    //    reach into the frozen composer) ───────────────────────────
    function safeMediaSrc(url) {
        const u = url == null ? '' : String(url).trim();
        return u && MEDIA_SRC_RE.test(u) ? u : '';
    }

    function safeHref(url) {
        const u = url == null ? '' : String(url).trim();
        return SAFE_HREF_RE.test(u) ? u : '';
    }

    function imageSrc(value, resolveImageSrc) {
        const raw = value == null ? '' : String(value).trim();
        if (!raw) return '';
        if (ATTACHMENT_RE.test(raw)) {
            // Asset-layer seam (phase 2): unresolved attachment references
            // render as "no image" rather than as a broken image.
            return typeof resolveImageSrc === 'function' ? safeMediaSrc(resolveImageSrc(raw)) : '';
        }
        return safeMediaSrc(raw);
    }

    /**
     * Discord's own "Today at 6:42 PM" for today, a plain local date-time
     * otherwise. `nowMs` is injected so the output is deterministic for a
     * given clock, and so the preview and the generator agree.
     */
    function fmtTimestamp(value, nowMs) {
        if (!value) return '';
        const when = new Date(value);
        if (isNaN(when.getTime())) return '';
        const clock = new Date(nowMs);
        const time = when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        const sameDay = when.getFullYear() === clock.getFullYear()
            && when.getMonth() === clock.getMonth()
            && when.getDate() === clock.getDate();
        if (sameDay) return 'Today at ' + time;
        return when.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' }) + ' ' + time;
    }

    /** The message header's own time: local, clock-injected, locale-free. */
    function fmtHeaderTime(nowMs) {
        const clock = new Date(nowMs);
        let h = clock.getHours();
        const ampm = h >= 12 ? 'PM' : 'AM';
        h = h % 12 || 12;
        return 'Today at ' + h + ':' + String(clock.getMinutes()).padStart(2, '0') + ' ' + ampm;
    }

    // ── Element helpers ──────────────────────────────────────────
    function h(doc, tag, className, stats) {
        const el = doc.createElement(tag);
        if (className) { el.className = className; if (stats) stats.attrWrites++; }
        if (stats) stats.nodesCreated++;
        return el;
    }

    function create(mount, options) {
        options = options || {};
        if (!mount || typeof mount.appendChild !== 'function') {
            throw new TypeError('preview.create needs a mount element');
        }
        if (typeof options.now !== 'function') {
            throw new TypeError('preview.create needs opts.now — the clock is injected, never read');
        }

        const doc = mount.ownerDocument || options.document;
        if (!doc || typeof doc.createElement !== 'function') {
            throw new TypeError('preview.create needs a document (mount.ownerDocument or opts.document)');
        }

        const renderMarkup = typeof options.renderMarkup === 'function' ? options.renderMarkup : markdown.render;
        const resolveImageSrc = options.resolveImageSrc;

        const stats = {
            nodesCreated: 0,
            nodesRemoved: 0,
            nodesMoved: 0,
            textWrites: 0,
            attrWrites: 0,
            markupWrites: 0,
            markdownRenders: 0,
            patches: 0,
        };

        // Per-node memo of the last markup INPUT that was rendered into it.
        // Keyed on the input, not the output, so an unchanged field costs
        // zero markdown parses — and never on the rendered HTML, because a
        // browser normalises what it parses and reading it back would lie.
        const markupKeys = new WeakMap();
        const markupResults = new WeakMap();

        let destroyed = false;
        let lastPayload = null;

        const ctx = {
            now: options.now,
            botIdentity: options.botIdentity || { name: 'Bot', avatar: null },
            lookups: options.lookups || {},
            lookupsVersion: options.lookupsVersion == null ? 0 : options.lookupsVersion,
            emptyText: options.emptyText || EMPTY_TEXT_DEFAULT,
        };

        // ── Counted DOM writes ────────────────────────────────────
        function setText(node, value) {
            const next = value == null ? '' : String(value);
            if (node.textContent === next) return false;
            node.textContent = next;
            stats.textWrites++;
            return true;
        }

        // `null`/`undefined` removes the attribute; an empty string is a real
        // value and is written as one (alt="" is not the same thing as no alt).
        function setAttr(node, name, value) {
            if (value == null) {
                if (node.getAttribute(name) === null) return false;
                node.removeAttribute(name);
                stats.attrWrites++;
                return true;
            }
            const next = String(value);
            if (node.getAttribute(name) === next) return false;
            node.setAttribute(name, next);
            stats.attrWrites++;
            return true;
        }

        function setClass(node, value) {
            const next = value || '';
            if (node.className === next) return false;
            node.className = next;
            stats.attrWrites++;
            return true;
        }

        /**
         * Write an element's TRAILING text — for the one place where v1 puts
         * a bare text node beside a child element (the author name). This is
         * the literal form of "patch the text node": the node is reused and
         * its `nodeValue` is updated; it is only created when missing.
         */
        function setTrailingText(el, value) {
            const next = value == null ? '' : String(value);
            const last = el.lastChild;
            if (last && last.nodeType === 3) {
                if (last.nodeValue === next) return false;
                last.nodeValue = next;
                stats.textWrites++;
                return true;
            }
            if (!next) return false;
            el.appendChild(doc.createTextNode(next));
            stats.textWrites++;
            return true;
        }

        function clearChildren(el) {
            while (el.firstChild) {
                el.removeChild(el.firstChild);
                stats.nodesRemoved++;
            }
        }

        /**
         * Render markdown into a LEAF element, or do nothing when this
         * node already holds the result of the same input. Returns the
         * render result so callers can read `isEmojiOnly`.
         */
        function renderInto(node, context, text, extra) {
            const input = context + '\u0000' + (text == null ? '' : String(text)) +
                          '\u0000' + ctx.lookupsVersion;
            if (markupKeys.get(node) === input) return markupResults.get(node) || null;
            stats.markdownRenders++;
            const result = renderMarkup(text, {
                context: context,
                lookups: ctx.lookups,
                checkEmojiOnly: !!(extra && extra.checkEmojiOnly),
            });
            node.innerHTML = result.html;
            markupKeys.set(node, input);
            markupResults.set(node, result);
            stats.markupWrites++;
            return result;
        }

        // ── Keyed list reconciliation ─────────────────────────────
        // `desired` is an ordered array of { key, build(), update(node) }.
        //
        // Note on counting moves: a reorder repositions an existing node with
        // insertBefore. The node OBJECT is untouched, but a browser reports
        // that as a removal+addition pair in MutationObserver records — which
        // is exactly why `nodesMoved` is a separate counter rather than being
        // folded into created/removed.
        // Existing children are matched by their data-key: a node whose key
        // is still present is REUSED (identity preserved), a node whose key
        // is gone is removed, and order is fixed with the minimum number of
        // moves. A second child carrying a key that was already seen is a
        // duplicate and is dropped deterministically.
        function reconcile(container, desired) {
            const existing = new Map();
            let node = container.firstChild;
            while (node) {
                const next = node.nextSibling;
                const key = node.getAttribute ? node.getAttribute('data-key') : null;
                if (key !== null && key !== undefined) {
                    if (existing.has(key)) {
                        container.removeChild(node);
                        stats.nodesRemoved++;
                    } else {
                        existing.set(key, node);
                    }
                }
                node = next;
            }

            const wanted = new Set();
            desired.forEach(d => wanted.add(d.key));
            existing.forEach((child, key) => {
                if (!wanted.has(key)) {
                    container.removeChild(child);
                    existing.delete(key);
                    stats.nodesRemoved++;
                }
            });

            let ref = container.firstChild;
            for (let i = 0; i < desired.length; i++) {
                const item = desired[i];
                let child = existing.get(item.key);
                const existed = !!child;
                if (!child) {
                    child = item.build();
                    child.setAttribute('data-key', item.key);   // a real attribute write: counted
                    stats.attrWrites++;
                    existing.set(item.key, child);
                }
                // EVERY desired node is updated, new or reused: build() only
                // creates the shell, so a fresh node gets its content from the
                // same code path a reused node does. That is what keeps the
                // mounted DOM and a patched DOM identical by construction.
                item.update(child);
                if (child !== ref) {
                    container.insertBefore(child, ref || null);
                    if (existed) stats.nodesMoved++;      // an existing node repositioned
                }
                ref = child.nextSibling;
            }
        }

        // ── Building blocks ───────────────────────────────────────
        // NB: a build() function creates ONLY the node it returns. Any child
        // that belongs to a reconciled list is created by that list's own
        // build(), otherwise the first reconcile would see an unkeyed child
        // and add a second copy beside it.
        function buildMessageNode() {
            return h(doc, 'div', 'eb-msg', stats);
        }

        function buildHeaderNode() {
            const header = h(doc, 'div', 'eb-msg-header', stats);
            header.appendChild(h(doc, 'span', 'eb-msg-name', stats));
            header.appendChild(h(doc, 'span', 'eb-msg-app-badge', stats));
            header.appendChild(h(doc, 'span', 'eb-msg-time', stats));
            return header;
        }

        function buildEmbedNode() {
            const card = h(doc, 'div', 'eb-preview-embed', stats);
            const fields = h(doc, 'div', 'eb-pe-fields', stats);
            card.__region = fields;                                // own property: not read elsewhere
            return card;
        }

        function buildFieldNode() {
            const card = h(doc, 'div', 'eb-pe-field', stats);
            card.appendChild(h(doc, 'div', 'eb-pe-field-name', stats));
            card.appendChild(h(doc, 'div', 'eb-pe-field-value', stats));
            return card;
        }

        // ── Message level ─────────────────────────────────────────
        function messageSpec(payload) {
            return {
                key: 'message',
                build: buildMessageNode,
                update: (node) => updateMessage(node, payload),
            };
        }

        function updateMessage(node, payload) {
            const identity = ctx.botIdentity || {};
            // v1 renders the bot avatar's url as given (it comes from the
            // server, not from the message), so this one is not run through
            // the media allowlist — matching v1 exactly.
            const avatarUrl = identity.avatar ? String(identity.avatar) : '';
            const avatarSpec = {
                key: avatarUrl ? 'avatar:img' : 'avatar:div',
                build: () => (avatarUrl ? h(doc, 'img', 'eb-msg-avatar', stats) : h(doc, 'div', 'eb-msg-avatar', stats)),
                update: (el) => {
                    if (!avatarUrl) return;
                    setAttr(el, 'src', avatarUrl);
                    setAttr(el, 'alt', '');
                },
            };
            const body = {
                key: 'body',
                build: () => h(doc, 'div', 'eb-msg-body', stats),
                update: (el) => updateBody(el, payload),
            };
            reconcile(node, [avatarSpec, body]);
        }

        function updateBody(body, payload) {
            const desired = [{
                key: 'header',
                build: buildHeaderNode,
                update: (header) => {
                    const identity = ctx.botIdentity || {};
                    setText(header.children[0], identity.name || 'Bot');
                    setText(header.children[1], 'APP');
                    setText(header.children[2], fmtHeaderTime(ctx.now()));
                },
            }];

            if (payload.content) {
                desired.push({
                    key: 'content',
                    build: () => h(doc, 'div', 'eb-msg-content', stats),
                    update: (el) => {
                        const result = renderInto(el, 'content', payload.content, { checkEmojiOnly: true });
                        const emojiOnly = !!(result && result.isEmojiOnly);
                        setClass(el, emojiOnly ? 'eb-msg-content eb-emoji-only-content' : 'eb-msg-content');
                    },
                });
            }

            (payload.embeds || []).forEach((embed, i) => {
                desired.push({
                    key: 'embed:' + embedKey(embed, i),
                    build: buildEmbedNode,
                    update: (node) => updateEmbed(node, embed),
                });
            });

            reconcile(body, desired);
        }

        function embedKey(embed, index) {
            // The real path always carries `key` (model ids). The positional
            // fallback exists only for a hand-built payload, and is a
            // documented degradation — never used by updateDocument.
            return embed && embed.key !== undefined && embed.key !== null
                ? String(embed.key) : 'index:' + index;
        }

        function fieldKey(field, index) {
            return field && field.key !== undefined && field.key !== null
                ? String(field.key) : 'index:' + index;
        }

        function accentOf(embed) {
            return embed.color === undefined || embed.color === null
                ? DEFAULT_ACCENT : model.colorToHex(embed.color);
        }

        // ── Embed card ────────────────────────────────────────────
        function updateEmbed(card, embed) {
            setAttr(card, 'style', 'border-left-color:' + accentOf(embed) + ';');

            const desired = [];
            const author = embed.author || {};
            const authorIcon = imageSrc(author.icon_url, resolveImageSrc);
            const authorHref = safeHref(author.url);

            if (author.name || authorIcon) {
                const iconSpec = {
                    key: 'icon',
                    // v1 gives this <img> no class — only an inline style.
                    build: () => h(doc, 'img', null, stats),
                    update: (icon) => {
                        setAttr(icon, 'src', authorIcon);
                        setAttr(icon, 'alt', '');
                        setAttr(icon, 'style', AUTHOR_ICON_STYLE);
                    },
                };
                const iconItems = authorIcon ? [iconSpec] : [];
                // v1's author block: [icon?] + name as bare text, wrapped in an
                // <a> ONLY when the author has a url. The shape is derived from
                // the DOM, so switching href on/off rebuilds just this region.
                desired.push({
                    key: 'author',
                    build: () => h(doc, 'div', 'eb-pe-author', stats),
                    update: (wrap) => {
                        const hasLink = !!wrap.firstChild && wrap.firstChild.tagName === 'A';
                        if (authorHref) {
                            if (!hasLink) clearChildren(wrap);
                            reconcile(wrap, [{
                                key: 'link',
                                build: () => h(doc, 'a', null, stats),
                                update: (link) => {
                                    setAttr(link, 'href', authorHref);
                                    setAttr(link, 'target', '_blank');
                                    setAttr(link, 'rel', 'noopener noreferrer');
                                    setAttr(link, 'style', AUTHOR_LINK_STYLE);
                                    reconcile(link, iconItems);
                                    setTrailingText(link, author.name || '');
                                },
                            }]);
                            return;
                        }
                        if (hasLink) clearChildren(wrap);      // plain text, never a fake link
                        reconcile(wrap, iconItems);
                        setTrailingText(wrap, author.name || '');
                    },
                });
            }

            if (embed.title) {
                const titleHref = safeHref(embed.url);
                desired.push({
                    key: 'title',
                    build: () => h(doc, 'div', 'eb-pe-title', stats),
                    update: (wrap) => {
                        if (!titleHref) {
                            if (wrap.firstChild && wrap.firstChild.tagName === 'A') clearChildren(wrap);
                            setText(wrap, embed.title);
                            return;
                        }
                        if (wrap.firstChild && wrap.firstChild.tagName !== 'A') setText(wrap, '');
                        reconcile(wrap, [{
                            key: 'link',
                            build: () => h(doc, 'a', null, stats),
                            update: (link) => {
                                setAttr(link, 'href', titleHref);
                                setAttr(link, 'target', '_blank');
                                setAttr(link, 'rel', 'noopener noreferrer');
                                setAttr(link, 'style', LINK_RESET);
                                setText(link, embed.title);
                            },
                        }]);
                    },
                });
            }

            if (embed.description) {
                desired.push({
                    key: 'description',
                    build: () => h(doc, 'div', 'eb-pe-desc', stats),
                    update: (el) => renderInto(el, 'description', embed.description),
                });
            }

            const fields = embed.fields || [];
            const hasFieldsKey = embed.fields !== undefined;
            if (hasFieldsKey) {
                // The region is created with the card, so it survives the
                // card losing all of its fields and keeps the fields' identity.
                desired.push({
                    key: 'fields',
                    build: () => card.__region,
                    update: (region) => {
                        reconcile(region, fields.map((field, i) => ({
                            key: 'field:' + fieldKey(field, i),
                            build: buildFieldNode,
                            update: (el) => updateField(el, field),
                        })));
                    },
                });
            }

            const imageUrl = imageSrc(embed.image && embed.image.url, resolveImageSrc);
            if (imageUrl) {
                desired.push({
                    key: 'image',
                    build: () => h(doc, 'img', 'eb-pe-image', stats),
                    update: (el) => { setAttr(el, 'src', imageUrl); setAttr(el, 'alt', ''); },
                });
            }

            const thumbUrl = imageSrc(embed.thumbnail && embed.thumbnail.url, resolveImageSrc);
            if (thumbUrl) {
                desired.push({
                    key: 'thumbnail',
                    build: () => h(doc, 'img', 'eb-pe-thumb', stats),
                    update: (el) => { setAttr(el, 'src', thumbUrl); setAttr(el, 'alt', ''); },
                });
            }

            const footer = embed.footer || {};
            const footerIcon = imageSrc(footer.icon_url, resolveImageSrc);
            const stamp = fmtTimestamp(embed.timestamp, ctx.now());
            if (footer.text || footerIcon || stamp) {
                const parts = [];
                if (footerIcon) parts.push({ key: 'footer-icon', kind: 'icon', value: footerIcon });
                if (footer.text) parts.push({ key: 'footer-text', kind: 'text', value: footer.text });
                if (stamp) parts.push({ key: 'footer-stamp', kind: 'stamp', value: stamp });
                desired.push({
                    key: 'footer',
                    build: () => h(doc, 'div', 'eb-pe-footer', stats),
                    update: (el) => {
                        const items = [];
                        parts.forEach((part, i) => {
                            items.push(part.kind === 'icon'
                                ? {
                                    key: part.key,
                                    build: () => h(doc, 'img', null, stats),
                                    update: (img) => {
                                        setAttr(img, 'src', part.value);
                                        setAttr(img, 'alt', '');
                                        setAttr(img, 'style', FOOTER_ICON_STYLE);
                                    },
                                }
                                : {
                                    key: part.key,
                                    build: () => h(doc, 'span', null, stats),
                                    update: (span) => setText(span, part.value),
                                });
                            if (i < parts.length - 1) {
                                items.push({
                                    key: 'footer-sep:' + i,
                                    build: () => h(doc, 'span', null, stats),
                                    update: (sep) => {
                                        setAttr(sep, 'style', SEPARATOR_STYLE);
                                        setText(sep, '\u2022');
                                    },
                                });
                            }
                        });
                        reconcile(el, items);
                    },
                });
            }

            reconcile(card, desired);
        }

        function updateField(card, field) {
            setAttr(card, 'style', field.inline ? FIELD_INLINE_STYLE : FIELD_FULL_STYLE);
            setText(card.children[0], field.name);
            renderInto(card.children[1], 'fieldValue', field.value);
        }

        // ── Public surface ────────────────────────────────────────
        function patch(payload) {
            if (destroyed) return;
            stats.patches++;
            lastPayload = payload;
            if (payload.content || (payload.embeds && payload.embeds.length)) {
                reconcile(mount, [messageSpec(payload)]);
            } else {
                reconcile(mount, [{
                    key: 'empty',
                    build: () => h(doc, 'div', 'eb-preview-empty', stats),
                    update: (el) => setText(el, ctx.emptyText),
                }]);
            }
        }

        /** The real boundary: a normalized document, through the one transform. */
        function updateDocument(document) {
            patch(model.toDiscordPayload(document, { withKeys: true }));
        }

        /** Lower-level entry point for tools and tests (payload already keyed). */
        function updatePayload(payload) {
            patch(payload || { embeds: [] });
        }

        function destroy() {
            if (destroyed) return;
            destroyed = true;
            // destroy() is a legitimate teardown, not a patch, but it still
            // destroys nodes — so it is counted, and the counters stay an
            // honest account of how much DOM the preview created and threw away.
            while (mount.firstChild) {
                mount.removeChild(mount.firstChild);
                stats.nodesRemoved++;
            }
        }

        return {
            updateDocument: updateDocument,
            updatePayload: updatePayload,
            stats: () => Object.assign({}, stats),
            resetStats: () => { Object.keys(stats).forEach(k => { stats[k] = 0; }); },
            context: () => Object.assign({}, ctx, { lookups: ctx.lookups }),
            // New lookups invalidate every memoised markup render (a mention
            // that could not resolve before may resolve now), so passing them
            // bumps the version automatically: the memo cannot go stale by
            // accident. Pass lookupsVersion explicitly to control it.
            setContext: (patchObj) => {
                const next = patchObj || {};
                if ('lookups' in next && !('lookupsVersion' in next)) ctx.lookupsVersion += 1;
                Object.assign(ctx, next);
            },
            lastPayload: () => lastPayload,
            isDestroyed: () => destroyed,
            destroy: destroy,
            mount: mount,
        };
    }

    NERO.embed.preview = {
        create: create,
        fmtTimestamp: fmtTimestamp,
        fmtHeaderTime: fmtHeaderTime,
        safeHref: safeHref,
        safeMediaSrc: safeMediaSrc,
        imageSrc: imageSrc,
        DEFAULT_ACCENT: DEFAULT_ACCENT,
    };
})(window.NERO);
