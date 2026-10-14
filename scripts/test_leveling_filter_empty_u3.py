"""U3 - empty filtered results: say "no matches", per table, without losing state.

Run:
    cd scripts
    OWNER_ID=1 PYTHONPATH=<repo root> python test_leveling_filter_empty_u3.py

The Rewards tab has one audience/level filter pair that governs four tables
(role, currency, XP boost, Shop). Filtering to something that matched nothing
hid every row and left the table as a bare header, which reads like a broken
table rather than an answer - and it was indistinguishable at a glance from a
table that simply has no rewards configured, which the loaders already announce
with their own "No ... configured yet" row.

U3 adds a per-table filtered-empty row, created by applyRewardFilter()'s new
syncFilterEmptyRows() pass. It appears only when a table HAS rows and every one
of them is filtered out, so the two empty states stay distinct. It reuses the
existing td.empty styling, is excluded from every count (it is not a
.reward-row), and is appended with insertAdjacentHTML so the rows the filter
just hid are not recreated and silently revealed.

What this file proves:

1. the server-rendered first paint is untouched (no such row without JS) and the
   four "configured yet" copies are unchanged;
2. BEHAVIOUR, by executing the served page script in node against a stub DOM:
   a filter that matches nothing announces itself in every affected table with
   the right colspan and wording; a filter that only empties SOME tables leaves
   the others alone (including the XP boost table, whose rows carry no audience
   and are therefore not part of the Member/Booster filter); "nothing
   configured" never turns into "no matches"; clearing the filter hides the
   message and brings the rows back; adding or deleting while filtered
   re-establishes the correct state after the table is re-rendered in place;
   and throughout, the tab, the filters, the hidden rows, the count badges and
   the summary line are preserved with no page reload;
3. scope: this is presentational and template-local - no API, storage, CSS,
   shared JS module, claim engine or /level code knows about it.
"""
import asyncio
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from phase1_support import GUILD, USER, execute, reset_database

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "dashboard" / "templates" / "systems" / "leveling.html"
MAIN_CSS = ROOT / "dashboard" / "static" / "css" / "main.css"
NERO_SELECT = ROOT / "dashboard" / "static" / "js" / "nero-select.js"
API_LEVELING = ROOT / "dashboard" / "api" / "leveling.py"
APP_MODULE = ROOT / "dashboard" / "app.py"
COGS_LEVELING = ROOT / "cogs" / "leveling.py"
LEVEL_CLAIMS = ROOT / "utils" / "level_claims.py"
NODE = "/usr/local/bin/node"

ADMIN_ID = USER + 500
CSRF = "u3-csrf"

# Role rewards: (level, role_id, audience). 10 is the only Booster-only one.
SEED = [(3, 600, "everyone"), (5, 601, "everyone"), (10, 602, "boosters")]
# Every other track has rows too, so a filter can empty all four tables at once.
CURRENCY = [{"id": 11, "level": 3, "currency": "balance", "amount": 250,
             "audience": "everyone"},
            {"id": 12, "level": 5, "currency": "balance", "amount": 500,
             "audience": "everyone"}]
BOOST = [{"id": 21, "level": 4, "multiplier": 2, "duration_hours": 12},
         {"id": 22, "level": 8, "multiplier": 3, "duration_hours": 24}]
SHOP = [{"id": 31, "level": 3, "item_id": 7, "name": "Ghost", "quantity": 2,
         "audience": "everyone"},
        {"id": 32, "level": 6, "item_id": 8, "name": "Nitro", "quantity": 1,
         "audience": "everyone"}]
TABLES = ["rewards-tbody", "currency-rewards-tbody", "boost-rewards-tbody",
          "shop-rewards-tbody"]
COLSPAN = {"rewards-tbody": 3, "currency-rewards-tbody": 4,
           "boost-rewards-tbody": 4, "shop-rewards-tbody": 4}
WORDING = {"rewards-tbody": "No role rewards match the current filters",
           "currency-rewards-tbody": "No currency rewards match the current filters",
           "boost-rewards-tbody": "No XP boost rewards match the current filters",
           "shop-rewards-tbody": "No Shop rewards match the current filters"}
CONFIGURED = {"rewards-tbody": "No role rewards configured yet",
              "currency-rewards-tbody": "No currency rewards configured yet",
              "boost-rewards-tbody": "No XP boost rewards configured yet",
              "shop-rewards-tbody": "No Shop rewards configured yet"}


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def page_script_in_window(html: str) -> str:
    start = html.find('id="content-area"')
    end = html.find('id="edit-modal"')
    window = html[start:end if end != -1 else len(html)]
    found = [m.group(1) for m in
             re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", window)]
    return max(found, key=len) if found else ""


def dashboard_client(user_id=ADMIN_ID, permission="admin"):
    import dashboard.app as dashboard

    dashboard.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
    execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
            "VALUES (?,?,?,1)", (GUILD, user_id, permission))
    client = dashboard.app.test_client()
    with client.session_transaction() as session:
        session.update(
            user={"id": user_id, "username": "U3 Tester", "avatar": None},
            guild_id=GUILD, guild_name="Guild", user_level=permission,
            expires_at=time.time() + 7200, csrf_token=CSRF)
    return client


NODE_HARNESS = r"""
const fs = require('fs');
const scenario = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = { errors: [], fetches: [], toasts: [], reloads: 0, switchTabCalls: 0,
              snaps: {} };
const nativeTimeout = setTimeout;
const settle = (ms) => new Promise(r => nativeTimeout(r, ms || 20));
process.on('unhandledRejection', e =>
    out.errors.push('unhandled: ' + ((e && e.message) || e)));

// ── stub DOM ──────────────────────────────────────────────────────────────
// The part that matters for U3 is modelled the way a real DOM behaves:
//   * assigning a tbody's innerHTML replaces its children, destroys any row it
//     held (so document.getElementById can no longer find it) and invalidates
//     the parsed-row cache, exactly like a loader re-render does;
//   * insertAdjacentHTML('beforeend', ...) APPENDS without touching the nodes
//     already there, so the rows the filter hid keep their display:none and
//     their object identity. Rewriting markup instead of appending would show
//     up here as every hidden row becoming visible again;
//   * an id that only exists once appended (the filtered-empty row) is reported
//     as missing until it is appended, and missing again after a re-render.
const TABS = ['leaderboard', 'config', 'rewards', 'bonuses', 'prestige'];
const TBODIES = ['rewards-tbody', 'currency-rewards-tbody',
                 'boost-rewards-tbody', 'shop-rewards-tbody'];
const els = {};
const appendedIds = new Set();
let cacheKey = null, cacheRows = [], cacheCells = [];

function makeEl(id) {
    const node = {
        id, value: '', textContent: '', style: {}, dataset: {}, title: '',
        className: '', children: [], options: [], disabled: false, isText: false,
        _html: '',
        classList: { contains: () => false, add() {}, remove() {}, toggle() {} },
        appendChild(c) { node.children.push(c); return c; },
        removeChild(c) { node.children = node.children.filter(x => x !== c); return c; },
        insertAdjacentHTML(pos, html) {
            if (pos !== 'beforeend') throw new Error('stub: only beforeend is modelled');
            if (/class="reward-row"|class="reward-role"/.test(html)) {
                throw new Error('stub: appending reward rows/cells is not modelled');
            }
            node._html += String(html);           // no invalidation: nodes survive
            String(html).replace(/id="([^"]+)"/g, (_, found) => {
                appendedIds.add(found);
                return '';
            });
        },
        querySelectorAll(sel) {
            if (sel === 'tr.reward-row') {
                return allRewardRows().filter(r => r._tbody === id);
            }
            if (sel === '.reward-role') {
                return allRoleCells().filter(c => c._tbody === id);
            }
            return [];
        },
        querySelector: () => null,
        addEventListener() {}, focus() {}, click() {}, remove() {},
        getAttribute: () => null, setAttribute() {},
    };
    Object.defineProperty(node, 'innerHTML', {
        get: () => node._html,
        set(v) {
            node._html = String(v);
            node.children.length = 0;
            if (TBODIES.indexOf(id) !== -1) {
                cacheKey = null;                              // rows are new nodes
                appendedIds.delete(id + '-filter-empty');      // appended row is gone
            }
        },
    });
    return node;
}
function el(id) { return els[id] || (els[id] = makeEl(id)); }
function newNode(tag) { const n = makeEl('(new ' + tag + ')'); n.tagName = tag; return n; }

function parseData(attrs) {
    const dataset = {};
    String(attrs).replace(/data-([\w-]+)="([^"]*)"/g, (_, k, v) => {
        dataset[k.replace(/-(\w)/g, (s2, c) => c.toUpperCase())] = v;
        return '';
    });
    return dataset;
}

// Appending the filtered-empty row changes a tbody's markup without replacing
// the rows already in it, so a naive re-parse would hand back brand-new row
// objects and the display:none the filter applied would appear to vanish. A
// real DOM keeps those nodes, and so does this: a parsed row or cell is reused
// when its own opening tag and position are unchanged, and only genuinely new
// markup gets new objects (which is what a loader's innerHTML rewrite does).
function rescan() {
    const key = TBODIES.map(id => (els[id] ? els[id]._html : '')).join('|');
    if (key === cacheKey) return;
    const prevRows = new Map();
    cacheRows.forEach(r => prevRows.set(r._sig, r));
    const prevCells = new Map();
    cacheCells.forEach(c => prevCells.set(c._sig, c));
    cacheKey = key;
    cacheRows = [];
    cacheCells = [];
    TBODIES.forEach(id => {
        const html = els[id] ? els[id]._html : '';
        const rowRe = /<tr class="reward-row"([^>]*)>/g;
        let m, n = 0;
        while ((m = rowRe.exec(html)) !== null) {
            const sig = id + '|row|' + (n++) + '|' + m[0];
            const prev = prevRows.get(sig);
            cacheRows.push(prev || { style: {}, dataset: parseData(m[1]),
                                     _tbody: id, _sig: sig });
        }
        const cellRe = /<span class="reward-role"([^>]*)>/g;
        n = 0;
        while ((m = cellRe.exec(html)) !== null) {
            const sig = id + '|cell|' + (n++) + '|' + m[0];
            const prev = prevCells.get(sig);
            if (prev) { cacheCells.push(prev); continue; }
            const cell = makeEl('reward-role ' + id + ' ' + cacheCells.length);
            cell.dataset = parseData(m[1]);
            cell._tbody = id;
            cell._sig = sig;
            cacheCells.push(cell);
        }
    });
}
function allRewardRows() { rescan(); return cacheRows; }
function allRoleCells() { rescan(); return cacheCells; }

function reportTables() {
    const report = {};
    TBODIES.forEach(id => {
        const html = els[id] ? els[id]._html : '';
        const all = allRewardRows().filter(r => r._tbody === id);
        const rowId = id + '-filter-empty';
        const marks = Array.from(html.matchAll(
            /<tr id="([^"]+)" class="filter-empty"><td colspan="(\d+)" class="empty">([^<]*)<\/td><\/tr>/g));
        const present = appendedIds.has(rowId);
        report[id] = {
            rows: all.length,
            visible: all.filter(r => r.style.display !== 'none').length,
            hidden: all.filter(r => r.style.display === 'none').length,
            configuredEmpty: (html.match(/class="empty">([^<]*configured yet[^<]*)</) || [])[1] || null,
            messageCount: marks.length,
            messageIds: marks.map(m => m[1]),
            colspan: marks.length ? Number(marks[0][2]) : null,
            text: marks.length ? marks[0][3] : null,
            present: present,
            shown: present && els[rowId] ? els[rowId].style.display !== 'none' : false,
        };
    });
    return report;
}
function reportRoles() {
    return allRewardRows().filter(r => r._tbody === 'rewards-tbody')
        .map(r => ({ level: r.dataset.level, audience: r.dataset.audience,
                     display: r.style.display || '' }));
}
function snap(name) {
    out.snaps[name] = {
        reloads: out.reloads,
        switchTabCalls: out.switchTabCalls,
        fetches: out.fetches.slice(),
        toasts: out.toasts.slice(),
        errors: out.errors.slice(),
        tables: reportTables(),
        roles: reportRoles(),
        badges: {
            roles: String(el('count-roles').textContent),
            currency: String(el('count-currency').textContent),
            boost: String(el('count-boost').textContent),
            shop: String(el('count-shop').textContent),
        },
        summary: el('reward-filter-summary').textContent,
        filterValues: { audience: el('reward-filter-audience').value,
                        level: el('reward-filter-level').value },
        tabs: { rewards: el('tab-content-rewards').style.display,
                leaderboard: el('tab-content-leaderboard').style.display },
    };
}

global.document = {
    // The filtered-empty row exists only once the script has appended it.
    getElementById(id) {
        if (/-filter-empty$/.test(id) && !appendedIds.has(id)) return null;
        return el(id);
    },
    createElement: newNode,
    createTextNode: (t) => ({ isText: true, textContent: String(t), children: [] }),
    querySelectorAll(sel) {
        if (sel === 'tr.reward-row') return allRewardRows();
        if (sel === '.reward-role') return allRoleCells();
        if (sel === '.tab-content') return TABS.map(t => el('tab-content-' + t));
        if (sel === '.tab-btn') return TABS.map(t => el('tab-' + t));
        return [];
    },
    querySelector: () => null,
    addEventListener() {},
    body: makeEl('body'),
};
global.window = global;
global.localStorage = {
    _d: {},
    getItem(k) { return this._d[k] === undefined ? null : this._d[k]; },
    setItem(k, v) { this._d[k] = String(v); },
    removeItem(k) { delete this._d[k]; },
    clear() { this._d = {}; },
};
global.sessionStorage = global.localStorage;
global.navigator = { userAgent: 'node-stub' };
global.__CURRENCY__ = { balance: { name: 'Coins', emoji: '🪙' },
                        diamonds: { name: 'Diamonds', emoji: '💎' } };

// The layout also loads static/js/dashboard.js, whose currencyLabel() this
// page's script calls when it renders the currency table. Evaluate the real
// file for fidelity; if it cannot run headlessly, fall back to a stub with the
// same contract (label text for a configured currency key, the key otherwise)
// and say so in the report.
try {
    (0, eval)(fs.readFileSync(process.argv[4], 'utf8'));
    out.dashboardJs = 'evaluated';
} catch (e) {
    out.dashboardJs = 'not evaluated: ' + e.message;
}
if (typeof global.currencyLabel !== 'function') {
    global.currencyLabel = (key) => {
        const info = (global.__CURRENCY__ || {})[key] || {};
        return String(info.name || key);
    };
    out.dashboardJs += ' (currencyLabel stubbed)';
}

global.location = { reload: () => { out.reloads += 1; }, href: '/leveling', hash: '' };
global.setTimeout = (fn, ms) => {
    try { fn(); } catch (e) { out.errors.push('timeout: ' + e.message); }
    return 0;
};
global.showToast = (msg, type) => out.toasts.push({ msg, type: type || 'success' });
global.showConfirm = (msg, fn) => fn;
global.htmx = { ajax() {}, refresh() {} };
global.$ = function (target) {
    const api = {
        val(v) {
            const node = target && target.id ? els[target.id] : null;
            if (v === undefined) return node ? node.value : '';
            if (node) node.value = v;
            return api;
        },
        trigger: () => api, select2: () => api, on: () => api, each: () => api,
        find: () => api, append: () => api, html: () => api, text: () => api,
    };
    return api;
};

function reply(body, status) {
    const code = status || 200;
    return Promise.resolve({ ok: code >= 200 && code < 300, status: code,
                             json: () => Promise.resolve(body) });
}
global.fetch = function (url, opts) {
    const method = (opts && opts.method) || 'GET';
    out.fetches.push(method + ' ' + url);
    if (url === '/api/guild/roles') return reply({ results: scenario.roles || [] });
    if (url === '/api/leveling/rewards' && method === 'GET') {
        return reply({ rewards: scenario.rewards });
    }
    if (url === '/api/leveling/currency-rewards') {
        return reply({ rewards: scenario.currency || [] });
    }
    if (url === '/api/leveling/boost-rewards') {
        return reply({ rewards: scenario.boost || [] });
    }
    if (url === '/api/leveling/shop-rewards') {
        return reply({ rewards: scenario.shop || [], products: [] });
    }
    if (url === '/api/leveling/reward' && method === 'POST') {
        const body = JSON.parse(opts.body);
        scenario.rewards.push({ id: ++scenario.nextId, level: body.level,
                                role_id: body.role_id,
                                audience: body.audience || 'everyone' });
        scenario.rewards.sort((a, b) => a.level - b.level);
        return reply({ success: true });
    }
    const gone = url.match(/^\/api\/leveling\/reward\/(\d+)$/);
    if (gone && method === 'DELETE') {
        scenario.rewards = scenario.rewards.filter(r => String(r.id) !== gone[1]);
        return reply({ success: true });
    }
    return reply({});
};

(async () => {
    const src = fs.readFileSync(process.argv[2], 'utf8');
    try { (0, eval)(src); } catch (e) { out.errors.push('init: ' + e.message); }
    await settle(40);

    // The admin is on the Rewards tab (the page script's own init defaults to
    // Leaderboard on load, which is unchanged and not what U3 is about).
    global.switchTab('rewards');
    const realSwitchTab = global.switchTab;
    global.switchTab = function (name) { out.switchTabCalls++; return realSwitchTab(name); };
    snap('onRewardsTab');

    for (const step of (scenario.steps || [])) {
        if (step.do === 'filter') {
            el('reward-filter-audience').value = step.audience || '';
            el('reward-filter-level').value = step.level == null ? '' : String(step.level);
            global.applyRewardFilter();
        } else if (step.do === 'add') {
            el('reward-level').value = String(step.level);
            el('reward-role-picker').value = String(step.role_id);
            el('reward-audience').value = step.audience || 'everyone';
            await global.addReward();
        } else if (step.do === 'delete') {
            await global.deleteReward(step.id);
        } else {
            out.errors.push('unknown step: ' + step.do);
        }
        await settle(40);
        snap(step.name);
    }

    snap('final');
    process.stdout.write(JSON.stringify(out));
})();
"""


def run_scenario(script: str, **cfg) -> dict:
    cfg.setdefault("roles", [{"id": "600", "name": "Level 3 Crew"},
                             {"id": "601", "name": "Boosters Only"},
                             {"id": "602", "name": "Deleted Role"},
                             {"id": "603", "name": "Late Addition"},
                             {"id": "99", "name": "High Roller"}])
    cfg.setdefault("rewards", [{"id": i + 1, "level": lv, "role_id": rid,
                                "audience": aud}
                               for i, (lv, rid, aud) in enumerate(SEED)])
    cfg.setdefault("currency", CURRENCY)
    cfg.setdefault("boost", BOOST)
    cfg.setdefault("shop", SHOP)
    cfg.setdefault("nextId", len(SEED))
    cfg.setdefault("steps", [])
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.js"
        src.write_text(script, encoding="utf-8")
        scen = Path(tmp) / "scenario.json"
        scen.write_text(json.dumps(cfg), encoding="utf-8")
        harness = Path(tmp) / "harness.js"
        harness.write_text(NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run([NODE, str(harness), str(src), str(scen),
                               str(ROOT / "dashboard" / "static" / "js" / "dashboard.js")],
                              capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise Fail(f"node harness crashed: {proc.stderr[-1500:]}")
        return json.loads(proc.stdout)


def state_of(result, step):
    snap = result["snaps"][step]
    if snap["errors"]:
        raise Fail(f"{step}: page script errors {snap['errors']}")
    return snap


def msg(snap, table):
    t = snap["tables"][table]
    return {k: t[k] for k in ("present", "shown", "text", "colspan", "messageCount")}


async def main():
    await reset_database()
    from utils.level_claims import ensure_tables

    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    for level, role_id, audience in SEED:
        execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
                "VALUES (?,?,?,?)", (GUILD, level, role_id, audience))

    # ── 1. the server-rendered page is untouched ──
    print("== 1. first paint and existing copy ==")
    client = dashboard_client()
    page = client.get("/leveling")
    check("the page still renders", page.status_code == 200, f"status={page.status_code}")
    html = page.get_data(as_text=True)
    head = html[:html.find("<script")]
    check("no filtered-empty row is baked into the server-rendered page",
          "filter-empty" not in head and "match the current filters" not in head)
    for table, copy in CONFIGURED.items():
        check(f"the '{table}' not-configured copy is unchanged", copy in html)
    script = page_script_in_window(html)
    check("the page script carries the new pass", bool(script)
          and "function syncFilterEmptyRows()" in script
          and "syncFilterEmptyRows();" in script)
    check("applyRewardFilter() is still the single filter entry point",
          script.count("function applyRewardFilter()") == 1
          and "updateFilterSummary(wantAudience, wantLevel);" in script
          and html.count('onchange="applyRewardFilter()"') == 1
          and html.count('oninput="applyRewardFilter()"') == 1)

    # ── 2. behaviour ──
    print("== 2. a filter that matches nothing ==")
    none_match = run_scenario(script, steps=[
        {"do": "filter", "name": "level99", "level": 99},
        {"do": "filter", "name": "cleared", "level": ""},
    ])
    print(f"   (layout script: {none_match.get('dashboardJs')})")
    base = state_of(none_match, "onRewardsTab")
    check("all four tables start with their rows visible and no message anywhere",
          all(base["tables"][t]["rows"] > 0 and base["tables"][t]["visible"] > 0
              and not base["tables"][t]["present"] for t in TABLES),
          str({t: base["tables"][t]["rows"] for t in TABLES}))

    filtered = state_of(none_match, "level99")
    for table in TABLES:
        t = filtered["tables"][table]
        check(f"{table}: every row is filtered out and the table says so",
              t["rows"] > 0 and t["visible"] == 0 and t["hidden"] == t["rows"]
              and t["shown"] and t["text"] == WORDING[table]
              and t["colspan"] == COLSPAN[table] and t["messageCount"] == 1,
              str(msg(filtered, table)))
    check("the message never masquerades as a reward row, so counts are untouched",
          filtered["badges"] == base["badges"]
          and all(not filtered["tables"][t]["configuredEmpty"] for t in TABLES),
          str(filtered["badges"]))
    check("the summary still reports the real numbers",
          filtered["summary"] == "0 of 9 reward row(s) match level 99",
          filtered["summary"])
    check("the tab, the filters and the page are untouched",
          filtered["tabs"] == {"rewards": "block", "leaderboard": "none"}
          and filtered["filterValues"] == {"audience": "", "level": "99"}
          and filtered["reloads"] == 0 and filtered["switchTabCalls"] == 0,
          str({k: filtered[k] for k in ("tabs", "filterValues", "reloads")}))
    check("hiding rows survived the append (no markup rewrite behind the filter)",
          all(r["display"] == "none" for r in filtered["roles"]), str(filtered["roles"]))

    cleared = state_of(none_match, "cleared")
    for table in TABLES:
        t = cleared["tables"][table]
        check(f"{table}: clearing the filter brings the rows back and hides the message",
              t["visible"] == t["rows"] and t["rows"] > 0
              and t["present"] and not t["shown"] and t["messageCount"] == 1,
              str(msg(cleared, table)))
    check("clearing the filter also clears the summary", cleared["summary"] == "",
          repr(cleared["summary"]))

    print("== 3. a filter that empties only some tables ==")
    boosters = run_scenario(script, steps=[{"do": "filter", "name": "boosters",
                                            "audience": "boosters"}])
    b = state_of(boosters, "boosters")
    check("the role table still has its Booster row, so it shows no message",
          b["tables"]["rewards-tbody"]["visible"] == 1
          and not b["tables"]["rewards-tbody"]["shown"]
          and [r["level"] for r in b["roles"] if r["display"] != "none"] == ["10"],
          str(msg(b, "rewards-tbody")) + str(b["roles"]))
    for table in ("currency-rewards-tbody", "shop-rewards-tbody"):
        t = b["tables"][table]
        check(f"{table} has only Member rows, so it says none match",
              t["rows"] == 2 and t["visible"] == 0 and t["shown"]
              and t["text"] == WORDING[table] and t["colspan"] == COLSPAN[table],
              str(msg(b, table)))
    t = b["tables"]["boost-rewards-tbody"]
    check("XP boost rows carry no audience, so the audience filter leaves them alone",
          t["rows"] == 2 and t["visible"] == 2 and not t["present"], str(msg(b, "boost-rewards-tbody")))
    check("counts and the summary are per the real rows, message rows excluded",
          b["badges"] == {"roles": "3", "currency": "2", "boost": "2", "shop": "2"}
          and b["summary"] == "3 of 9 reward row(s) match Booster Rewards",
          f"{b['badges']} {b['summary']}")

    print("== 4. nothing configured is not the same as nothing matching ==")
    empty = run_scenario(script, rewards=[], currency=[], boost=[], shop=[],
                         steps=[{"do": "filter", "name": "level99", "level": 99}])
    e = state_of(empty, "level99")
    for table in TABLES:
        t = e["tables"][table]
        check(f"{table}: with no rewards at all it keeps saying 'configured yet'",
              t["rows"] == 0 and t["configuredEmpty"] == CONFIGURED[table]
              and not t["present"] and not t["shown"] and t["messageCount"] == 0,
              str(msg(e, table)) + repr(t["configuredEmpty"]))
    check("an empty table never claims a filter hid something",
          all(not e["tables"][t]["present"] for t in TABLES)
          and e["badges"] == {"roles": "0", "currency": "0", "boost": "0", "shop": "0"},
          str(e["badges"]))

    print("== 5. add and delete while a filter is active ==")
    added = run_scenario(script, steps=[
        {"do": "filter", "name": "level99", "level": 99},
        {"do": "add", "name": "addedMatching", "level": 99, "role_id": 603,
         "audience": "everyone"},
    ])
    a0 = state_of(added, "level99")
    a1 = state_of(added, "addedMatching")
    check("before the add the role table says nothing matches",
          a0["tables"]["rewards-tbody"]["shown"]
          and a0["tables"]["rewards-tbody"]["visible"] == 0,
          str(msg(a0, "rewards-tbody")))
    check("a re-render in place drops the stale message row with the old markup",
          a1["tables"]["rewards-tbody"]["messageCount"] <= 1, str(msg(a1, "rewards-tbody")))
    check("adding a row that matches the filter clears the message and shows it",
          a1["tables"]["rewards-tbody"]["rows"] == 4
          and a1["tables"]["rewards-tbody"]["visible"] == 1
          and not a1["tables"]["rewards-tbody"]["shown"]
          and [r["level"] for r in a1["roles"] if r["display"] != "none"] == ["99"],
          str(a1["roles"]))
    check("the count badge follows the table, not the message",
          a1["badges"]["roles"] == "4", str(a1["badges"]))
    check("the other tables keep their own messages through someone else's add",
          all(a1["tables"][t]["shown"] and a1["tables"][t]["text"] == WORDING[t]
              for t in ("currency-rewards-tbody", "shop-rewards-tbody",
                        "boost-rewards-tbody")),
          str({t: msg(a1, t)["shown"] for t in TABLES}))
    check("no reload and no tab switch anywhere in that flow",
          a1["reloads"] == 0 and a1["switchTabCalls"] == 0
          and a1["tabs"] == {"rewards": "block", "leaderboard": "none"},
          str(a1["tabs"]))

    deleted = run_scenario(script, steps=[
        {"do": "filter", "name": "boosters", "audience": "boosters"},
        {"do": "delete", "name": "deletedLastMatch", "id": 3},
    ])
    d0 = state_of(deleted, "boosters")
    d1 = state_of(deleted, "deletedLastMatch")
    check("before the delete the single Booster row is visible with no message",
          d0["tables"]["rewards-tbody"]["visible"] == 1
          and not d0["tables"]["rewards-tbody"]["present"], str(msg(d0, "rewards-tbody")))
    check("deleting the last matching row re-establishes the message after re-render",
          d1["tables"]["rewards-tbody"]["rows"] == 2
          and d1["tables"]["rewards-tbody"]["visible"] == 0
          and d1["tables"]["rewards-tbody"]["shown"]
          and d1["tables"]["rewards-tbody"]["text"] == WORDING["rewards-tbody"]
          and d1["tables"]["rewards-tbody"]["colspan"] == 3
          and d1["tables"]["rewards-tbody"]["messageCount"] == 1,
          str(msg(d1, "rewards-tbody")))
    check("the filter itself is still in force after that delete",
          d1["filterValues"] == {"audience": "boosters", "level": ""}
          and all(r["display"] == "none" for r in d1["roles"])
          and d1["badges"]["roles"] == "2", str(d1["roles"]) + str(d1["badges"]))
    check("the deleted row is really gone, not just hidden by a message",
          [r["level"] for r in d1["roles"]] == ["3", "5"], str(d1["roles"]))

    # ── 6. scope ──
    print("== 6. scope guards ==")
    template = TEMPLATE.read_text(encoding="utf-8")
    check("the empty state is template-local presentation",
          template.count("function syncFilterEmptyRows()") == 1
          and template.count("syncFilterEmptyRows();") == 1
          and template.count('class="filter-empty"') == 1)
    css = MAIN_CSS.read_text(encoding="utf-8")
    check("it reuses the existing td.empty styling and adds no CSS",
          "td.empty" in css and ".filter-empty" not in css)
    for path, label in ((API_LEVELING, "dashboard/api/leveling.py"),
                        (APP_MODULE, "dashboard/app.py"),
                        (NERO_SELECT, "static/js/nero-select.js"),
                        (COGS_LEVELING, "cogs/leveling.py"),
                        (LEVEL_CLAIMS, "utils/level_claims.py")):
        text = path.read_text(encoding="utf-8")
        check(f"{label} knows nothing about the filtered-empty state",
              "filter-empty" not in text and "syncFilterEmptyRows" not in text
              and "match the current filters" not in text)
    check("counts and the summary still count reward rows only",
          "badge.textContent = tbody.querySelectorAll('tr.reward-row').length;" in template
          and "const rows = Array.from(document.querySelectorAll('tr.reward-row'));"
          in template)
    check("U1's in-place refresh and U2's role names are intact",
          "loadRewards();" in template and "applyRoleNames();" in template
          and "location.reload" not in template
          and "showConfirm('Delete Level ${r.level} reward?', () => deleteReward(${r.id}))"
          in template)
    check("no reward storage, entitlement or claim behaviour is reachable from here",
          "INSERT" not in template and "UPDATE " not in template
          and "DELETE FROM" not in template)

    print("ALL U3 FILTERED-EMPTY CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1)
