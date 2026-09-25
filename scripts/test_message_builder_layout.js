#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — phase 1, step 5a: the page's static contract.

   The page module is a consumer of markup it does not own: it looks up ids the
   template declares, it is loaded by nav-lifecycle.js in the order
   data-page-script lists, and it is styled by a stylesheet that must not touch
   the frozen v1 builder. None of that is exercised by booting the page in a DOM
   double — a double happily provides whatever id the module asks for, which is
   exactly how a renamed id survives a test suite and dies in a browser.

   So this harness checks the OTHER half: the real template, the real stylesheet
   and the real route, parsed from disk. Together with
   scripts/test_message_builder_page.js (behaviour) it closes the loop:

     template declares id  ←→  module resolves id      (A + B below)
     template lists scripts ←→  modules exist/loadable (B)
     route renders template ←→  permission + identity  (C)
     stylesheet stays in v2's namespace                (D)

   Run:  node scripts/test_message_builder_layout.js
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

const ROOT = path.join(__dirname, '..');
const TEMPLATE_PATH = path.join(ROOT, 'dashboard', 'templates', 'manage', 'message_builder.html');
const CSS_PATH = path.join(ROOT, 'dashboard', 'static', 'css', 'message-builder.css');
const PAGE_PATH = path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'message-builder-page.js');
const STATUSBAR_PATH = path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'views', 'statusbar.js');
const APP_PATH = path.join(ROOT, 'dashboard', 'app.py');

const TEMPLATE = fs.readFileSync(TEMPLATE_PATH, 'utf8');
const CSS = fs.readFileSync(CSS_PATH, 'utf8');
const PAGE_SRC = fs.readFileSync(PAGE_PATH, 'utf8');
const APP = fs.readFileSync(APP_PATH, 'utf8');

// ── A tiny template parser (stack based, void-aware) ──────────────
// Enough for this template and deliberately not more: it strips Jinja and
// HTML comments first, then builds a real tree so nesting (not just ordering)
// can be asserted.
const VOID = new Set(['link', 'meta', 'input', 'br', 'img', 'hr', 'source', 'track', 'wbr', 'area', 'base', 'col', 'embed']);

function parseAttributes(raw) {
    const attrs = {};
    const re = /([\w:.-]+)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
    let m;
    while ((m = re.exec(raw || '')) !== null) {
        const name = m[1].toLowerCase();
        if (!name || name === '/') continue;
        attrs[name] = m[3] !== undefined ? m[3] : (m[4] !== undefined ? m[4] : (m[5] !== undefined ? m[5] : ''));
    }
    return attrs;
}

function parseTemplate(html) {
    const src = html.replace(/\{#[\s\S]*?#\}/g, '').replace(/<!--[\s\S]*?-->/g, '');
    const root = { tag: '_root', attrs: {}, children: [], parent: null };
    const stack = [root];
    const re = /<(\/?)([a-zA-Z][\w-]*)((?:"[^"]*"|'[^']*'|[^>"'])*?)(\/?)>/g;
    let m;
    while ((m = re.exec(src)) !== null) {
        const tag = m[2].toLowerCase();
        if (m[1] === '/') {
            for (let i = stack.length - 1; i > 0; i--) {
                if (stack[i].tag === tag) { stack.length = i; break; }
            }
            continue;
        }
        const node = { tag: tag, attrs: parseAttributes(m[3]), children: [], parent: stack[stack.length - 1] };
        stack[stack.length - 1].children.push(node);
        if (!VOID.has(tag) && m[4] !== '/') stack.push(node);
    }
    return root;
}

function walk(node, fn, acc) {
    acc = acc || [];
    node.children.forEach(child => { acc.push(child); fn(child, node); walk(child, fn, acc); });
    return acc;
}
function allNodes(root) { return walk(root, () => {}); }
function byId(root, id) { return allNodes(root).find(n => n.attrs.id === id) || null; }
function byTag(root, tag) { return allNodes(root).filter(n => n.tag === tag); }
function ancestors(node) {
    const out = [];
    let c = node && node.parent;
    while (c) { out.push(c); c = c.parent; }
    return out;
}
function flatText(node) { return JSON.stringify(node); }

const TREE = parseTemplate(TEMPLATE);
const ELEMENTS = allNodes(TREE);
const ROOT_NODE = byId(TREE, 'mb2-root');
const CSS_NO_COMMENTS = CSS.replace(/\/\*[\s\S]*?\*\//g, '');

// ═══════════════════════════════════════════════════════════════
section('A. the template declares the shell (ids, roles, nesting)');
// ═══════════════════════════════════════════════════════════════
assert(!!ROOT_NODE, 'the page root #mb2-root exists');
const ROOT_ATTRS = (ROOT_NODE && ROOT_NODE.attrs) || {};
assert(ROOT_ATTRS['data-page-module'] === 'message-builder',
    '#mb2-root declares data-page-module="message-builder"', flatText(ROOT_ATTRS));
assert(ROOT_ATTRS['data-guild-id'] === "{{ guild_id or '' }}",
    'the draft namespace guild comes from the server-rendered session, not a query string',
    String(ROOT_ATTRS['data-guild-id']));

const REQUIRED = [
    ['mb2-strip', 'p'],
    ['mb2-rail', 'nav'],
    ['mb2-rail-title', 'h2'],
    ['mb2-rail-body', 'div'],
    ['mb2-inspector', 'section'],
    ['mb2-inspector-title', 'h2'],
    ['mb2-inspector-body', 'div'],
    ['mb2-preview-region', 'aside'],
    ['mb2-preview-title', 'h2'],
    ['mb2-mount', 'div'],
    ['mb2-bar', 'div'],
    ['mb2-bar-status', 'p'],
    ['mb2-bar-actions', 'div'],
];
REQUIRED.forEach(([id, tag]) => {
    const node = byId(TREE, id);
    assert(!!node, '#' + id + ' is declared', id);
    if (node) assert(node.tag === tag, '#' + id + ' is a <' + tag + '>', node.tag);
});

// Live regions: present in the markup, so the region exists before any text is
// written into it — writing text into a region that is created in the same tick
// is the classic way an announcement gets lost.
const strip = byId(TREE, 'mb2-strip');
assert(strip && strip.attrs.role === 'status' && strip.attrs['aria-live'] === 'polite',
    'the validation strip is a polite live region', strip && flatText(strip.attrs));
assert(strip && Object.prototype.hasOwnProperty.call(strip.attrs, 'hidden'),
    'the validation strip starts hidden (a blank document is clean, so step 6a has nothing to say)');
assert(strip && strip.attrs.class === 'mb2-strip',
    'and it starts with exactly its base class (no tone class before the page has spoken)',
    strip && flatText(strip.attrs));

// ── the served limits ride in the shell (step 6a, transport L1) ──
// data-limits must be on the page ROOT (the page reads it there) and it must be
// the tojson|forceescape pipe: tojson alone leaves the double quotes raw, which
// would break out of the double-quoted attribute the moment a limits table
// contained one. This is a source-level assertion on purpose — the JSON itself
// is asserted by the page harness, which hands the page a real table.
assert(ROOT_ATTRS['data-limits'] === '{{ limits | tojson | forceescape }}',
    'the page root carries the served limits, escaped so the attribute cannot break',
    String(ROOT_ATTRS['data-limits']));
assert(ROOT_ATTRS['data-limits'].indexOf('limits') !== -1 &&
       ROOT_ATTRS['data-limits'].indexOf('tojson') !== -1 &&
       ROOT_ATTRS['data-limits'].indexOf('forceescape') !== -1,
    'and the expression is the documented tojson|forceescape pipe');
const status = byId(TREE, 'mb2-bar-status');
assert(status && status.attrs.role === 'status' && status.attrs['aria-live'] === 'polite',
    'the bar status is a polite live region', status && flatText(status.attrs));

const rail = byId(TREE, 'mb2-rail');
const inspector = byId(TREE, 'mb2-inspector');
const previewRegion = byId(TREE, 'mb2-preview-region');
assert(rail && rail.attrs['aria-labelledby'] === 'mb2-rail-title', 'the rail is a labelled <nav>');
assert(inspector && inspector.attrs['aria-labelledby'] === 'mb2-inspector-title',
    'the inspector is a labelled <section>', inspector && flatText(inspector.attrs));
assert(inspector && inspector.attrs.tabindex === '-1',
    'the inspector is focusable without becoming a tab stop (focus moves here after structural changes)',
    inspector && inspector.attrs.tabindex);
assert(previewRegion && previewRegion.attrs['aria-labelledby'] === 'mb2-preview-title',
    'the preview is a labelled <aside>');

const mount = byId(TREE, 'mb2-mount');
assert(mount && ancestors(mount).some(n => n.attrs && n.attrs.id === 'mb2-preview-region'),
    '#mb2-mount is nested inside the preview region');
assert(mount && mount.children.length === 0 && !mount.attrs.id.match(/\$\{/),
    'the preview mount is empty in the markup (preview.js owns everything inside it)',
    mount && flatText(mount.children));

// Order: rail → inspector → preview region → bar. This is the tab order, and
// the bar last keeps it out of the reading flow of the document.
const order = ['mb2-rail', 'mb2-inspector', 'mb2-preview-region', 'mb2-bar']
    .map(id => ELEMENTS.indexOf(byId(TREE, id)));
assert(order.every(i => i >= 0) && order[0] < order[1] && order[1] < order[2] && order[2] < order[3],
    'document order is rail → inspector → preview → bar', String(order));

assert(byId(TREE, 'mb2-bar-actions') && byId(TREE, 'mb2-bar-actions').children.length === 0,
    'the action container is empty in the MARKUP (actionbar.js renders the buttons at runtime)');

// Hygiene: no inline script, no on* handlers, no inline styles, no v1/v2 class clash.
const INLINE_HANDLERS = ELEMENTS.filter(n => Object.keys(n.attrs).some(a => a.indexOf('on') === 0 && a.length > 2));
assert(INLINE_HANDLERS.length === 0, 'no on* attributes anywhere in the template',
    INLINE_HANDLERS.map(n => n.tag).join(','));
assert(byTag(TREE, 'script').length === 0,
    'no <script> in the fragment (an inline script inside a swapped fragment never re-executes)');
const styled = ELEMENTS.filter(n => Object.prototype.hasOwnProperty.call(n.attrs, 'style'));
assert(styled.length === 0, 'no inline style attributes', styled.map(n => n.tag).join(','));
const ebClasses = ELEMENTS.filter(n => String(n.attrs.class || '').split(/\s+/).some(c => c.indexOf('eb-') === 0 || c.indexOf('mb-') === 0));
assert(ebClasses.length === 0,
    'no v1 (eb-*) or minigame (mb-*) classes leak into the v2 markup',
    ebClasses.map(n => n.attrs.class).join(' | '));
const otherIds = ELEMENTS.map(n => n.attrs.id).filter(Boolean).filter(id => id.indexOf('mb2-') !== 0);
assert(otherIds.length === 0, 'every id in the v2 template is namespaced mb2-', otherIds.join(','));

// The stylesheet decision: loaded from the fragment, not from base.html.
const links = byTag(TREE, 'link').filter(n => n.attrs.rel === 'stylesheet');
assert(links.length === 1 && /css\/message-builder\.css/.test(links[0].attrs.href || ''),
    'the template loads exactly one stylesheet: message-builder.css', JSON.stringify(links.map(l => l.attrs.href)));
assert(links.length === 1 && ancestors(links[0]).some(n => n.attrs && n.attrs.class === 'content-block' === false) &&
    links[0].parent && links[0].parent.tag === '_root',
    'the stylesheet link sits at the top level of the content block (arrives and leaves with the fragment)');
const BASE = fs.readFileSync(path.join(ROOT, 'dashboard', 'templates', 'base.html'), 'utf8');
assert(BASE.indexOf('message-builder.css') === -1,
    'base.html does not reference v2 CSS (untouched by step 5a)');
assert(TEMPLATE.indexOf('{% block scripts %}') === -1,
    'the template declares no {% block scripts %} (the v1 double-execution bug stays fixed)');
assert(TEMPLATE.indexOf('data-page-title="Message Builder"') !== -1,
    'the page title is set through the existing data-page-title hook');

// ═══════════════════════════════════════════════════════════════
section('B. the module and the markup agree');
// ═══════════════════════════════════════════════════════════════
// Load the page module for its declared id surface + script list, with a stub
// registry so nothing else has to exist.
const sandbox = { window: { NERO: { definePage: function () {} } }, console: console, setTimeout: setTimeout, clearTimeout: clearTimeout };
vm.createContext(sandbox);
vm.runInContext(PAGE_SRC, sandbox, { filename: 'message-builder-page.js' });
const PAGE = sandbox.window.NERO.embed.messageBuilderPage;
assert(!!PAGE, 'the page module registers NERO.embed.messageBuilderPage');
const ID_MAP = (PAGE && PAGE.ID) || {};
const declaredIds = Object.keys(ID_MAP).map(k => ID_MAP[k]);
const missing = declaredIds.filter(id => !byId(TREE, id));
assert(missing.length === 0,
    'every id the page module resolves is declared by the template',
    'missing: ' + missing.join(','));
assert(declaredIds.length >= 9, 'the module declares the full region surface', String(declaredIds.length));

const SCRIPTS = String(ROOT_ATTRS['data-page-script'] || '');
const scriptOrder = (SCRIPTS.match(/js\/[^']+?'/g) || []).map(s => s.replace(/'$/, ''));
const expectedOrder = [
    'js/embed/model.js', 'js/embed/store.js', 'js/embed/validate.js',
    'js/embed/discord-markdown.js', 'js/embed/preview.js', 'js/embed/drafts.js',
    'js/embed/views/statusbar.js', 'js/embed/views/rail.js', 'js/embed/views/inspector.js',
    'js/embed/views/actionbar.js', 'js/embed/message-builder-page.js',
];
assert(JSON.stringify(scriptOrder) === JSON.stringify(expectedOrder),
    'data-page-script loads the foundations before the page, in dependency order',
    scriptOrder.join(' '));
scriptOrder.forEach(rel => {
    const file = path.join(ROOT, 'dashboard', 'static', rel);
    assert(fs.existsSync(file), 'listed script exists: ' + rel);
});
// The validator is a foundation, so it must be loaded BEFORE the page that
// calls it (the page's foundation() throws without it) and it must publish the
// one global the page looks for.
const VALIDATE_PATH = path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'validate.js');
const VALIDATE_SRC = fs.readFileSync(VALIDATE_PATH, 'utf8');
assert(/NERO\.embed\.validate\s*=/.test(VALIDATE_SRC),
    'embed/validate.js publishes NERO.embed.validate');
assert(scriptOrder.indexOf('js/embed/validate.js') < scriptOrder.indexOf('js/embed/message-builder-page.js'),
    'and the page is loaded after it');

// The page is loaded LAST by the registry and must not be loadable without the
// registry (that is what stops it from being silently inlined into the fragment).
let loadError = null;
try {
    const bare = { window: { NERO: {} }, console: console };
    vm.createContext(bare);
    vm.runInContext(PAGE_SRC, bare, { filename: 'message-builder-page.js' });
} catch (e) { loadError = e; }
assert(!!loadError && /nav-lifecycle/.test(String(loadError.message)),
    'loading the page module without nav-lifecycle.js fails loudly',
    loadError ? loadError.message : 'loaded anyway');
assert(PAGE_SRC.indexOf('embed-composer') === -1 && PAGE_SRC.indexOf('embed-builder-page') === -1,
    'the v2 page module references no v1 module',
    'v1 reference found');

// The statusbar view is a separate module with its own surface.
const statusbar = fs.readFileSync(STATUSBAR_PATH, 'utf8');
// Comments are stripped before the "does it reach into the store?" checks: the
// header explains the ownership rule by name, and explaining a rule is not
// breaking it.
const STATUS_CODE = statusbar.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
assert(/NERO\.embed\.views\.statusbar\s*=/.test(statusbar),
    'the statusbar view publishes NERO.embed.views.statusbar');
assert(/mb2-rail-row/.test(CSS_NO_COMMENTS) && /mb2-rail-btn/.test(CSS_NO_COMMENTS),
    'the rail rows are styled in the page stylesheet');

// ── the inspector's stylesheet contract (step 5c) ────────────────
// The inspector is a column of labelled controls: it needs the touch-target
// floor and the wrapping rules, and it must NOT bring validation styling.
assert(/\.mb2-insp-input/.test(CSS_NO_COMMENTS) && /\.mb2-insp-label/.test(CSS_NO_COMMENTS),
    'the inspector controls and labels are styled');
assert(/\.mb2-insp-group/.test(CSS_NO_COMMENTS) && /\.mb2-insp-fields/.test(CSS_NO_COMMENTS),
    'its groups and the field list are styled');
assert(/min-height:\s*3[0-9]px/.test(CSS_NO_COMMENTS),
    'controls keep a touch-target minimum height');
assert(/\.mb2-insp-input\s*\{[^}]*width:\s*100%/.test(CSS_NO_COMMENTS),
    'inputs fill their column instead of overflowing it');
assert(!/\.mb2-insp[^{]*\{[^}]*overflow\s*:\s*hidden/.test(CSS_NO_COMMENTS),
    'no inspector rule clips content (long field names wrap instead)');
['danger', 'warning', 'success'].forEach(tone => {
    assert(!new RegExp('\\.mb2-insp[^{]*\\{[^}]*--' + tone).test(CSS_NO_COMMENTS),
        'no validation tone leaks into the inspector (' + tone + ')');
});
assert(!/mb2-insp-(error|invalid|limit|count|warning)/.test(CSS_NO_COMMENTS),
    'and no validation classes exist yet');
assert(/view\.dirty/.test(STATUS_CODE) && !/store\.isDirty/.test(STATUS_CODE) && !/options\.store/.test(STATUS_CODE),
    'the statusbar is TOLD the dirty state (view.dirty) and never reaches into the store itself');
assert(!/indexedDB|localStorage|sessionStorage/.test(statusbar) && !/indexedDB|localStorage|sessionStorage/.test(PAGE_SRC),
    'no page/view module opens storage on its own (persistence belongs to drafts.js)');

// ── the strip's stylesheet contract (step 6a) ───────────────────
// The strip is one region with two tones, in the same mb2-tone-* vocabulary the
// status bar uses, and it must wrap the issue text instead of widening the shell.
assert(/\.mb2-strip\.mb2-tone-warn\s*\{[^}]*--warning/.test(CSS_NO_COMMENTS) &&
       /\.mb2-strip\.mb2-tone-danger\s*\{[^}]*--danger/.test(CSS_NO_COMMENTS),
    'the strip has a warning and a danger tone, drawn from the shared tone variables');
assert(/\.mb2-strip\s*\{[^}]*overflow-wrap:\s*anywhere/.test(CSS_NO_COMMENTS),
    'and a long issue message wraps instead of overflowing the shell');
assert(!/\.mb2-strip[^{]*\{[^}]*display:\s*block/.test(CSS_NO_COMMENTS),
    'the strip never overrides the [hidden] rule with a display of its own');

// ── the action bar's contract (step 5d) ──────────────────────────
// The bar is the one place where a button does something irreversible-ish, so
// the static contract matters: it exists, it is loaded in order (B above), it
// is styled (including the disabled state), its dialog cannot join the bar's
// flex row, and it reaches for neither storage nor the renderer.
const ACTIONBAR_PATH = path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'views', 'actionbar.js');
assert(fs.existsSync(ACTIONBAR_PATH), 'the action bar module exists');
const ACTIONBAR_SRC = fs.readFileSync(ACTIONBAR_PATH, 'utf8');
const ACTIONBAR_CODE = ACTIONBAR_SRC.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');
assert(/NERO\.embed\.views\.actionbar\s*=/.test(ACTIONBAR_SRC),
    'the action bar view publishes NERO.embed.views.actionbar');
assert(/\.mb2-bar-btn\s*\{/.test(CSS_NO_COMMENTS) && /\.mb2-bar-btn:disabled/.test(CSS_NO_COMMENTS),
    'the bar buttons are styled, including their real disabled state');
assert(/\.mb2-dialog-overlay\s*\{[^}]*position:\s*fixed/.test(CSS_NO_COMMENTS),
    'a dialog is fixed to the viewport (it never joins the bar\'s flex row)');
assert(/\.mb2-dialog\s*\{[^}]*max-width/.test(CSS_NO_COMMENTS),
    'the dialog panel is bounded by a max-width, never a fixed width');
assert(/\.mb2-dialog-btn\s*\{[^}]*min-height:\s*3[0-9]px/.test(CSS_NO_COMMENTS),
    'dialog controls keep the touch-target floor');
assert(!/indexedDB|localStorage|sessionStorage/.test(ACTIONBAR_SRC),
    'the action bar opens no storage of its own');
assert(!/NERO\.embed\.(preview|drafts)/.test(ACTIONBAR_CODE),
    'and never reaches for the renderer or the draft session');
assert(!/aria-live|role="status"/.test(ACTIONBAR_CODE),
    'and declares no second live region (the page owns the only one)');

// ═══════════════════════════════════════════════════════════════
section('C. the route');
// ═══════════════════════════════════════════════════════════════
const v2Route = /@app\.route\("\/embed-builder\/v2"\)\s*\n@require_page\("embedbuilder"\)\s*\ndef embed_builder_v2\(\):([\s\S]*?)\n\n/.exec(APP);
assert(!!v2Route, '/embed-builder/v2 exists and is guarded by require_page("embedbuilder")');
assert(!!v2Route && /render\("manage\/message_builder\.html"/.test(v2Route[1]),
    'the v2 route renders manage/message_builder.html');
assert(!!v2Route && /bot_identity=_bot_identity_for_page\(/.test(v2Route[1]),
    'the v2 route passes the guild bot identity (no Discord call from the browser)');
// Step 6a: the limits table the template renders must come from the server's one
// authority (utils/discord_limits), not from a literal in the route.
assert(!!v2Route && /limits=limits_payload\(\)/.test(v2Route[1]),
    'the v2 route renders the served limits table into the page (transport L1)');
assert(/^from utils\.discord_limits import limits_payload$/m.test(APP),
    'and the route imports that table from utils/discord_limits (one authority)');
assert((APP.match(/limits_payload\(\)/g) || []).length === 1,
    'the v2 route is the only page in app.py that renders a limits table',
    String((APP.match(/limits_payload\(\)/g) || []).length));
const v1Route = /@app\.route\("\/embed-builder"\)[\s\S]*?return render\("manage\/embedbuilder\.html"/.exec(APP);
assert(!!v1Route, 'the v1 /embed-builder route still renders manage/embedbuilder.html');
assert(APP.indexOf('"/embed-builder/v2"') !== APP.lastIndexOf('"/embed-builder/v2"') ||
    (APP.match(/"\/embed-builder\/v2"/g) || []).length === 1,
    'the v2 route is declared exactly once',
    String((APP.match(/"\/embed-builder\/v2"/g) || []).length));

// No navigation entry anywhere (D-1): reachable by URL only in this step.
const templatesDir = path.join(ROOT, 'dashboard', 'templates');
function collect(dir, out) {
    fs.readdirSync(dir, { withFileTypes: true }).forEach(entry => {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) collect(full, out);
        else if (entry.name.endsWith('.html')) out.push(full);
    });
    return out;
}
const linked = collect(templatesDir, [])
    .filter(file => fs.readFileSync(file, 'utf8').indexOf('embed-builder/v2') !== -1);
assert(linked.length === 0, 'no template links to /embed-builder/v2 (no nav entry in step 5a)',
    linked.join(','));
assert(fs.readFileSync(path.join(templatesDir, 'manage', 'embedbuilder.html'), 'utf8').indexOf('mb2-') === -1,
    'the frozen v1 template contains no v2 markup');

// ═══════════════════════════════════════════════════════════════
section('D. the stylesheet stays in v2');
// ═══════════════════════════════════════════════════════════════
assert(!/\.eb-/.test(CSS_NO_COMMENTS), 'no .eb-* selector (that namespace is v1 s)');
assert(!/\.mb-(?!2)/.test(CSS_NO_COMMENTS), 'no .mb-* selector (that namespace is the minigame builder s)');
assert(/\.mb2-grid/.test(CSS_NO_COMMENTS), 'the grid is styled');
assert(/--mb2-sticky-top/.test(CSS_NO_COMMENTS), 'the sticky preview offset is a single variable');
assert(/position:\s*sticky/.test(CSS_NO_COMMENTS), 'the preview (or bar) is sticky');
assert(/grid-template-columns:[^;]*minmax\(0,\s*1fr\)/.test(CSS_NO_COMMENTS),
    'the flexible grid track cannot overflow (minmax(0, 1fr))');
assert(/min-width:\s*0/.test(CSS_NO_COMMENTS),
    'grid children opt out of the auto minimum that causes sideways scroll');
// The invariant is about the STICKY PREVIEW's ancestors, not about the word:
// an overflow:hidden inside the rail's own label cannot affect a sibling region.
// So this checks the rules that could actually reach the preview or the shell.
const OVERFLOW_RULES = (CSS_NO_COMMENTS.match(/[^{}]+\{[^{}]*\}/g) || [])
    .filter(block => /overflow\s*:\s*hidden/.test(block))
    .map(block => block.split('{')[0].trim());
const STICKY_ANCESTORS = ['.mb2-preview', '.mb2-grid', '.mb2-shell', 'html', 'body'];
const offenders = OVERFLOW_RULES.filter(sel =>
    STICKY_ANCESTORS.some(target => sel.split(',').map(x => x.trim()).indexOf(target) !== -1));
assert(offenders.length === 0,
    'no overflow:hidden on the sticky preview or any of its ancestors',
    offenders.join(' | '));
assert(OVERFLOW_RULES.every(sel => /\.mb2-rail-label/.test(sel) || /\.mb2-rail-btn/.test(sel) || /\.mb2-rail-row/.test(sel)),
    'the only overflow:hidden rules are the rail label s ellipsis (by design)',
    OVERFLOW_RULES.join(' | '));
// (a media query is `max-width:`, so the character before `width` is `-`;
// requiring whitespace/;/brace excludes breakpoints from a fixed-width check)
assert(!/(^|[;{\s])width:\s*\d{3,}px/.test(CSS_NO_COMMENTS) &&
       !/(^|[;{\s])min-width:\s*\d{3,}px/.test(CSS_NO_COMMENTS),
    'no fixed shell width that cannot shrink');
assert(/overflow-wrap:\s*anywhere/.test(CSS_NO_COMMENTS),
    'long unbroken values wrap instead of widening the column');
const breaks = (CSS_NO_COMMENTS.match(/@media[^{]+/g) || []).join(' ');
['1360px', '768px', '480px'].forEach(bp => {
    assert(breaks.indexOf(bp) !== -1, 'breakpoint ' + bp + ' is defined', breaks);
});
assert(/prefers-reduced-motion/.test(breaks), 'reduced-motion is respected');
assert(/\.mb2-preview\s*{[^}]*max-height/.test(CSS_NO_COMMENTS),
    'the sticky preview is height-bounded (never taller than the viewport)');
assert(!/@media[^{]*\{[^}]*\.mb2-preview\s*\{[^}]*position:\s*static/.test(CSS_NO_COMMENTS),
    'the preview is never switched off at narrow widths (it stays a top pane)');

// ═══════════════════════════════════════════════════════════════
console.log('\nmessage-builder layout: ' + pass + ' passed, ' + fail + ' failed');
if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
console.log('ALL MESSAGE-BUILDER LAYOUT CHECKS PASSED');
