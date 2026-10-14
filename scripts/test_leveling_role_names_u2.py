"""U2 - Level Reward role names: readable when resolvable, honest when not.

Run:
    cd scripts
    OWNER_ID=1 PYTHONPATH=<repo root> python test_leveling_role_names_u2.py

The Level Rewards table stored bare Discord role IDs. An ID says nothing to an
admin, and - the actual defect - a reward pointing at a role that has been
deleted renders exactly like a healthy one, so the table quietly promises a
payout that can never happen.

U2 resolves names on the client from the endpoint the role pickers already use
(`GET /api/guild/roles`, see dashboard/static/js/nero-select.js), cached once
per page load. No backend route, storage column, API contract or entitlement
rule changes, and the raw ID stays on screen in every state.

What this file proves:

1. the API surface is untouched: `GET /api/leveling/rewards` still answers with
   exactly id/level/role_id/audience (no name field), and `/api/guild/roles`
   keeps the documented fail-soft shape U2 consumes;
2. the server-rendered first paint still works with no JS at all (raw ID inside
   <code>) and now carries the data-role-id hook the page script hydrates;
3. BEHAVIOUR, by executing the served page script in node against a stub DOM:
   a known role shows its name, an absent role is reported as not found, and an
   unavailable role list is reported as unverifiable - never as deleted - for
   every way that lookup can fail (200 + error payload, 200 + empty results,
   403, 500, thrown fetch). Also: names survive an in-place add and delete, the
   role list is fetched once per page load, a hostile role name can only ever
   become text, and the hydration also covers the server-rendered rows when the
   list endpoint itself is down;
4. scope: Prestige / bonus / blacklist role cells are untouched, the shared
   picker module and the CSS are untouched, and no reward, audience, claim or
   /level code references any of this.
"""
import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from phase1_support import GUILD, USER, execute, reset_database, rows

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "dashboard" / "templates" / "systems" / "leveling.html"
NERO_SELECT = ROOT / "dashboard" / "static" / "js" / "nero-select.js"
MAIN_CSS = ROOT / "dashboard" / "static" / "css" / "main.css"
API_CORE = ROOT / "dashboard" / "api" / "core.py"
COGS_LEVELING = ROOT / "cogs" / "leveling.py"
LEVEL_CLAIMS = ROOT / "utils" / "level_claims.py"
NODE = "/usr/local/bin/node"

ADMIN_ID = USER + 500
CSRF = "u2-csrf"

# (level, role_id, audience). 602 is deliberately missing from the guild's role
# list below, which is what a deleted role looks like from the dashboard.
SEED = [(3, 600, "everyone"), (5, 601, "everyone"), (10, 602, "boosters")]
ADDED = {"level": 7, "role_id": "603", "audience": "boosters"}
ROLES = [
    {"id": "600", "name": "Level 3 Crew", "color": "#7c5cbf", "position": 3},
    {"id": "601", "name": "Boosters Only", "color": None, "position": 2},
    {"id": "603", "name": "Late Addition", "color": "#57f287", "position": 1},
]
EVIL_NAME = '<img src=x onerror="alert(1)"> & "quoted"'


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def page_script_in_window(html: str) -> str:
    """The largest inline script inside #content-area (the window the dashboard
    page-script harness uses), i.e. the script the browser actually runs."""
    start = html.find('id="content-area"')
    end = html.find('id="edit-modal"')
    window = html[start:end if end != -1 else len(html)]
    found = [m.group(1) for m in
             re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", window)]
    return max(found, key=len) if found else ""


def rewards_tbody_of(html: str) -> str:
    """The server-rendered role-reward rows exactly as the browser receives them."""
    m = re.search(r'<tbody id="rewards-tbody">([\s\S]*?)</tbody>', html)
    return m.group(1) if m else ""


def dashboard_client(user_id=ADMIN_ID, permission="admin"):
    import dashboard.app as dashboard

    dashboard.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)
    execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
            "VALUES (?,?,?,1)", (GUILD, user_id, permission))
    client = dashboard.app.test_client()
    with client.session_transaction() as session:
        session.update(
            user={"id": user_id, "username": "U2 Tester", "avatar": None},
            guild_id=GUILD, guild_name="Guild", user_level=permission,
            expires_at=time.time() + 7200, csrf_token=CSRF)
    return client


NODE_HARNESS = r"""
const fs = require('fs');
const scenario = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = { errors: [], fetches: [], toasts: [], confirms: [], reloads: 0,
              switchTabCalls: 0, roleFetches: 0, snaps: {} };
const nativeTimeout = setTimeout;
const settle = (ms) => new Promise(r => nativeTimeout(r, ms || 20));
process.on('unhandledRejection', e =>
    out.errors.push('unhandled: ' + ((e && e.message) || e)));

// ── stub DOM ──────────────────────────────────────────────────────────────
// Faithful where this page's script depends on it: elements carry style,
// dataset, title, textContent and children; a tbody's innerHTML assignment
// replaces its children (and detaches any row or role cell it held, exactly as
// a real DOM does, so document.getElementById can no longer find them);
// querySelectorAll answers 'tr.reward-row' and '.reward-role' from that markup,
// document-wide and per tbody, with stable object identity while the markup is
// unchanged so filters and rendered names are observed on the same nodes the
// script mutated.
const TABS = ['leaderboard', 'config', 'rewards', 'bonuses', 'prestige'];
const TBODIES = ['rewards-tbody', 'currency-rewards-tbody',
                 'boost-rewards-tbody', 'shop-rewards-tbody'];
const els = {};
let cacheKey = null, cacheRows = [], cacheCells = [];

function makeEl(id) {
    const node = {
        id, value: '', textContent: '', style: {}, dataset: {}, title: '',
        className: '', children: [], options: [], disabled: false, isText: false,
        classList: { contains: () => false, add() {}, remove() {}, toggle() {} },
        appendChild(c) { node.children.push(c); return c; },
        removeChild(c) { node.children = node.children.filter(x => x !== c); return c; },
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
        addEventListener() {}, insertAdjacentHTML() {}, focus() {}, click() {},
        remove() {}, getAttribute: () => null, setAttribute() {},
    };
    let html = '';
    Object.defineProperty(node, 'innerHTML', {
        get: () => html,
        set(v) {
            html = String(v);
            node.children.length = 0;
            // Only a table body rewrite changes what the document contains.
            if (TBODIES.indexOf(id) !== -1) cacheKey = null;
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

function rescan() {
    const key = TBODIES.map(id => (els[id] ? els[id].innerHTML : '')).join('|');
    if (key === cacheKey) return;
    cacheKey = key;
    cacheRows = [];
    cacheCells = [];
    TBODIES.forEach(id => {
        const html = els[id] ? els[id].innerHTML : '';
        const rowRe = /<tr class="reward-row"([^>]*)>/g;
        let m;
        while ((m = rowRe.exec(html)) !== null) {
            cacheRows.push({ style: {}, dataset: parseData(m[1]), _tbody: id });
        }
        const cellRe = /<span class="reward-role"([^>]*)>/g;
        while ((m = cellRe.exec(html)) !== null) {
            const cell = makeEl('reward-role ' + id + ' ' + cacheCells.length);
            cell.dataset = parseData(m[1]);
            cell._tbody = id;
            cacheCells.push(cell);
        }
    });
}
function allRewardRows() { rescan(); return cacheRows; }
function allRoleCells() { rescan(); return cacheCells; }

function reportRows() {
    return allRewardRows().filter(r => r._tbody === 'rewards-tbody')
        .map(r => ({ audience: r.dataset.audience, level: r.dataset.level,
                     id: r.dataset.id, display: r.style.display || '' }));
}
function reportCells() {
    return allRoleCells().filter(c => c._tbody === 'rewards-tbody').map(c => {
        const kids = c.children || [];
        const label = kids[0] || {};
        const last = kids[kids.length - 1] || {};
        return {
            roleId: c.dataset.roleId,
            labelClass: label.className || '',
            labelText: label.textContent == null ? '' : String(label.textContent),
            separatorIsText: !!(kids[1] && kids[1].isText),
            codeText: last.textContent == null ? '' : String(last.textContent),
            kids: kids.length,
            title: c.title || '',
            assignedHtml: c.innerHTML,
        };
    });
}
function snap(name) {
    out.snaps[name] = {
        reloads: out.reloads,
        switchTabCalls: out.switchTabCalls,
        roleFetches: out.roleFetches,
        fetches: out.fetches.slice(),
        toasts: out.toasts.slice(),
        confirms: out.confirms.slice(),
        errors: out.errors.slice(),
        rows: reportRows(),
        cells: reportCells(),
        tbody: els['rewards-tbody'] ? els['rewards-tbody'].innerHTML : null,
        badge: els['count-roles'] ? String(els['count-roles'].textContent) : null,
        summary: els['reward-filter-summary']
            ? els['reward-filter-summary'].textContent : null,
        tabs: { rewards: el('tab-content-rewards').style.display,
                leaderboard: el('tab-content-leaderboard').style.display },
    };
}

global.document = {
    getElementById: el,
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
global.__CURRENCY__ = { balance: { name: 'Coins' }, diamonds: { name: 'Diamonds' } };
global.location = { reload: () => { out.reloads += 1; }, href: '/leveling', hash: '' };
// Immediate, so a stray setTimeout(() => location.reload(), 600) is caught.
global.setTimeout = (fn, ms) => {
    try { fn(); } catch (e) { out.errors.push('timeout: ' + e.message); }
    return 0;
};
global.showToast = (msg, type) => out.toasts.push({ msg, type: type || 'success' });
global.showConfirm = (msg, fn) => { out.confirms.push(msg); return fn; };
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
    return Promise.resolve({
        ok: code >= 200 && code < 300, status: code,
        json: () => Promise.resolve(body),
    });
}
global.fetch = function (url, opts) {
    const method = (opts && opts.method) || 'GET';
    out.fetches.push(method + ' ' + url);
    if (url === '/api/guild/roles') {
        out.roleFetches++;
        const cfg = scenario.roles || {};
        if (cfg.throw) return Promise.reject(new Error('network unreachable'));
        return reply(cfg.body === undefined ? {} : cfg.body, cfg.status);
    }
    if (url === '/api/leveling/rewards' && method === 'GET') {
        const cfg = scenario.list || {};
        if (cfg.throw) return Promise.reject(new Error('network unreachable'));
        if (cfg.status && cfg.status !== 200) return reply({}, cfg.status);
        return reply({ rewards: scenario.rewards });
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
    // The server-rendered rows the browser starts with, when the scenario says
    // the page was rendered with rewards already configured.
    if (scenario.seedTbody) el('rewards-tbody').innerHTML = scenario.seedTbody;

    const src = fs.readFileSync(process.argv[2], 'utf8');
    try { (0, eval)(src); } catch (e) { out.errors.push('init: ' + e.message); }
    await settle(40);
    snap('afterInit');

    // The admin moves to the Rewards tab and filters to Booster rewards, the
    // same starting point U1 is verified from.
    global.switchTab('rewards');
    const realSwitchTab = global.switchTab;
    global.switchTab = function (name) { out.switchTabCalls++; return realSwitchTab(name); };
    el('reward-filter-audience').value = 'boosters';
    el('reward-filter-level').value = '';
    global.applyRewardFilter();

    // add a reward whose role IS in the guild list
    el('reward-level').value = String(scenario.add.level);
    el('reward-role-picker').value = scenario.add.role_id;
    el('reward-audience').value = scenario.add.audience;
    await global.addReward();
    await settle(40);
    snap('afterAdd');

    // delete it again
    await global.deleteReward(scenario.nextId);
    await settle(40);
    snap('afterDelete');

    snap('final');
    process.stdout.write(JSON.stringify(out));
})();
"""


def run_scenario(script: str, **cfg) -> dict:
    cfg.setdefault("roles", {"body": {"results": ROLES}})
    cfg.setdefault("list", {})
    cfg.setdefault("rewards", [{"id": i + 1, "level": lv, "role_id": rid,
                                "audience": aud}
                               for i, (lv, rid, aud) in enumerate(SEED)])
    cfg.setdefault("nextId", len(SEED))
    cfg.setdefault("add", ADDED)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.js"
        src.write_text(script, encoding="utf-8")
        scen = Path(tmp) / "scenario.json"
        scen.write_text(json.dumps(cfg), encoding="utf-8")
        harness = Path(tmp) / "harness.js"
        harness.write_text(NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run([NODE, str(harness), str(src), str(scen)],
                              capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise Fail(f"node harness crashed: {proc.stderr[-1500:]}")
        return json.loads(proc.stdout)


def cell_of(snap, role_id):
    for c in snap["cells"]:
        if str(c["roleId"]) == str(role_id):
            return c
    return None


async def main():
    await reset_database()
    from utils.level_claims import ensure_tables

    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    for level, role_id, audience in SEED:
        execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
                "VALUES (?,?,?,?)", (GUILD, level, role_id, audience))

    # ── 1. the API surface U2 builds on is unchanged ──
    print("== 1. API contracts preserved ==")
    client = dashboard_client()
    listed = client.get("/api/leveling/rewards")
    check("GET /api/leveling/rewards still answers 200 for a level admin",
          listed.status_code == 200, f"status={listed.status_code}")
    payload = listed.get_json()["rewards"]
    check("it still returns exactly id/level/role_id/audience - no name field was added",
          payload and all(set(r) == {"id", "level", "role_id", "audience"} for r in payload),
          str(sorted(payload[0]) if payload else []))
    check("role IDs are still stored and served as stored, not rewritten to names",
          [str(r["role_id"]) for r in payload] == [str(rid) for _, rid, _ in SEED]
          and [r["level"] for r in payload] == [lv for lv, _, _ in SEED],
          str([(r["level"], r["role_id"], r["audience"]) for r in payload]))

    # The endpoint talks to Discord, so exercise the documented fail-soft branch
    # deterministically instead of depending on the network: no bot token means
    # 200 + {"results": [], "error": ...}, which is exactly one of the payloads
    # the table must report as "unverifiable" rather than "deleted". (With a
    # token set but Discord unreachable the same call raises and Flask answers
    # 500 - covered by the node scenarios below.)
    core_text = API_CORE.read_text(encoding="utf-8")
    saved_token = os.environ.get("DISCORD_TOKEN")
    os.environ["DISCORD_TOKEN"] = ""
    try:
        roles_resp = client.get("/api/guild/roles")
    finally:
        if saved_token is None:
            os.environ.pop("DISCORD_TOKEN", None)
        else:
            os.environ["DISCORD_TOKEN"] = saved_token
    check("GET /api/guild/roles keeps its fail-soft shape (200 + results + error)",
          roles_resp.status_code == 200
          and roles_resp.get_json().get("results") == []
          and roles_resp.get_json().get("error") == "BOT_TOKEN not set",
          f"status={roles_resp.status_code} body={roles_resp.get_json()}")
    check("its success payload still carries the id + name the table reads",
          '"id":       r["id"]' in core_text and '"name":     r["name"]' in core_text
          and 'f"Discord {resp.status_code}"' in core_text)
    check("the roles endpoint is the picker's own, still guild-scoped and permitted",
          '@api_bp.route("/guild/roles")' in core_text
          and "@require_api_permission(LEVEL_MODERATOR)" in core_text)
    check("it was not coupled to the reward tables to serve this feature",
          "leveling_rewards" not in core_text)

    # ── 2. server-rendered first paint ──
    print("== 2. server-rendered first paint ==")
    page = client.get("/leveling")
    check("the page still renders", page.status_code == 200, f"status={page.status_code}")
    html = page.get_data(as_text=True)
    tbody = rewards_tbody_of(html)
    hooks_in_tbody = tbody.count('class="reward-role"')
    check("every server-rendered row carries the data-role-id hydration hook",
          hooks_in_tbody == len(SEED)
          and all(f'data-role-id="{rid}"' in tbody for _, rid, _ in SEED),
          f"hooks={hooks_in_tbody}")
    check("with no JS at all the row still shows the stored ID (no regression)",
          all(f'<span class="reward-role" data-role-id="{rid}"><code>{rid}</code></span>'
              in tbody for _, rid, _ in SEED), tbody.strip().splitlines()[1].strip()[:90])
    check("no name, badge or health claim is baked into the server-rendered row",
          "Role not found" not in tbody and "Name unavailable" not in tbody
          and "role-name" not in tbody)
    check("row identity, audience and delete behaviour are untouched",
          all(f'data-level="{lv}"' in tbody and f'data-audience="{aud}"' in tbody
              for lv, _, aud in SEED)
          and tbody.count("showConfirm('Delete Level") == len(SEED)
          and tbody.count("deleteReward(") == len(SEED))
    check("the booster badge is still server-rendered for the booster reward",
          'badge badge-booster' in tbody and "⭐ Booster Reward" in tbody)

    script = page_script_in_window(html)
    check("the page script is the one the browser runs", bool(script)
          and "applyRoleNames" in script and "loadRoleNames" in script)

    # ── 3. behaviour, executing the served page script in node ──
    print("== 3. role names in the live table ==")
    healthy = run_scenario(script, seedTbody=tbody)
    init = healthy["snaps"]["afterInit"]
    check("the role path ran clean in the stub DOM", not init["errors"], str(init["errors"]))

    check("a role in the guild list shows its readable name",
          cell_of(init, 600)["labelText"] == "@Level 3 Crew"
          and cell_of(init, 600)["labelClass"] == "role-name",
          str(cell_of(init, 600)))
    check("a booster-only role shows its name too, next to its booster badge",
          cell_of(init, 601)["labelText"] == "@Boosters Only"
          and "⭐ Booster Reward" in init["tbody"], str(cell_of(init, 601)))
    check("the stored ID stays visible beside the name in the existing code chip",
          cell_of(init, 600)["codeText"] == "600"
          and cell_of(init, 600)["separatorIsText"]
          and cell_of(init, 600)["kids"] == 3
          and cell_of(init, 600)["assignedHtml"] == "",  # name is a node, never markup
          str(cell_of(init, 600)))

    missing = cell_of(init, 602)
    check("a role absent from a good list is flagged, not shown as healthy",
          missing["labelText"] == "Role not found"
          and missing["labelClass"] == "badge badge-danger", str(missing))
    check("the flag says why, and still shows the ID so the row can be fixed or deleted",
          "most likely deleted" in missing["title"] and "602" in missing["title"]
          and missing["codeText"] == "602", missing["title"])
    check("a not-found role never renders as a plain name",
          not missing["labelText"].startswith("@"), str(missing["labelText"]))

    check("the row itself is otherwise untouched - level, audience, delete",
          len(init["rows"]) == len(SEED)
          and all(r["id"] for r in init["rows"])
          and init["tbody"].count("deleteReward(") == len(SEED), str(init["rows"]))
    check("resolving names caused no reload and no tab switch",
          init["reloads"] == 0 and init["switchTabCalls"] == 0
          and init["tabs"] == {"rewards": "none", "leaderboard": "block"},
          str({k: init[k] for k in ("reloads", "switchTabCalls", "tabs")}))
    check("the role list is fetched once per page load, however many tables refresh",
          healthy["snaps"]["final"]["roleFetches"] == 1,
          f"roleFetches={healthy['snaps']['final']['roleFetches']} "
          f"fetches={healthy['snaps']['final']['fetches']}")

    added = healthy["snaps"]["afterAdd"]
    check("a reward added in place gets its name resolved like the others",
          cell_of(added, ADDED["role_id"])["labelText"] == "@Late Addition",
          str(cell_of(added, ADDED["role_id"])))
    check("adding still leaves the admin on the Rewards tab with the filter applied",
          added["switchTabCalls"] == 0 and added["reloads"] == 0
          and added["tabs"] == {"rewards": "block", "leaderboard": "none"}
          and all(r["display"] == "none" for r in added["rows"]
                  if r["audience"] == "everyone"), str(added["tabs"]))
    check("the not-found flag survives an unrelated add",
          cell_of(added, 602)["labelText"] == "Role not found", str(cell_of(added, 602)))
    check("counts and the filter summary are unaffected by name resolution",
          str(added["badge"]) == str(len(SEED) + 1)
          and "2 of 4 reward row(s) match Booster Rewards" == added["summary"],
          f"badge={added['badge']} summary={added['summary']}")

    deleted = healthy["snaps"]["afterDelete"]
    check("deleting in place re-resolves the remaining rows and keeps the flags",
          cell_of(deleted, ADDED["role_id"]) is None
          and cell_of(deleted, 600)["labelText"] == "@Level 3 Crew"
          and cell_of(deleted, 602)["labelText"] == "Role not found"
          and deleted["reloads"] == 0 and deleted["switchTabCalls"] == 0,
          str([c["roleId"] for c in deleted["cells"]]))

    # the same lookup failing in every way it can fail
    failures = {
        "200 with an error payload (no bot token / Discord refused)": {
            "roles": {"body": {"results": [], "error": "BOT_TOKEN not set"}}},
        "200 with an empty role list": {"roles": {"body": {"results": []}}},
        "403 (caller lacks the permission)": {
            "roles": {"status": 403, "body": {"success": False, "error": "Forbidden"}}},
        "500 from the dashboard": {"roles": {"status": 500, "body": {}}},
        "a thrown fetch (Discord unreachable)": {"roles": {"throw": True}},
    }
    for label, cfg in failures.items():
        res = run_scenario(script, seedTbody=tbody, **cfg)
        snap = res["snaps"]["afterInit"]
        cells = [cell_of(snap, rid) for _, rid, _ in SEED]
        check(f"unavailable role list ({label}) => unverifiable, never 'deleted'",
              all(c and c["labelText"] == "Name unavailable"
                  and c["labelClass"] == "badge badge-warning" for c in cells),
              str([(c or {}).get("labelText") for c in cells]))
        check(f"unavailable role list ({label}) => IDs stay on screen and explain themselves",
              all(c["codeText"] == str(rid) for c, (_, rid, _) in zip(cells, SEED))
              and all("could not be verified" in c["title"] for c in cells),
              cells[0]["title"] if cells and cells[0] else "")
        check(f"unavailable role list ({label}) => rows, filters, tab and toasts unaffected",
              len(snap["rows"]) == len(SEED) and snap["reloads"] == 0
              and not snap["errors"]
              and res["snaps"]["final"]["roleFetches"] == 1,
              f"rows={len(snap['rows'])} errors={snap['errors']}")

    # the server-rendered rows must also get names when the list endpoint works
    # but the reward list endpoint does not (loadRewards bails out early).
    firstpaint = run_scenario(script, seedTbody=tbody, list={"status": 500})
    fp = firstpaint["snaps"]["afterInit"]
    check("when the in-place refresh cannot run, the server-rendered rows are still named",
          cell_of(fp, 600)["labelText"] == "@Level 3 Crew"
          and cell_of(fp, 602)["labelText"] == "Role not found"
          and len(fp["rows"]) == len(SEED), str(fp["cells"]))

    evil = run_scenario(
        script, seedTbody=tbody,
        roles={"body": {"results": [{"id": "600", "name": EVIL_NAME}]}})
    ev = cell_of(evil["snaps"]["afterInit"], 600)
    check("a hostile role name can only ever become text, never markup",
          ev["labelText"] == "@" + EVIL_NAME      # it survives, as text
          and ev["labelClass"] == "role-name"     # on a plain label node
          and ev["assignedHtml"] == "",           # nothing was ever set as HTML
          str(ev))
    check("no cell in the table assigned HTML while resolving names",
          all(c["assignedHtml"] == "" for c in evil["snaps"]["afterInit"]["cells"]),
          str([c["assignedHtml"] for c in evil["snaps"]["afterInit"]["cells"]]))
    check("no role name is interpolated into the row markup anywhere",
          "<img" not in evil["snaps"]["afterInit"]["tbody"]
          and "onerror" not in evil["snaps"]["afterInit"]["tbody"]
          and "<img" not in evil["snaps"]["final"]["tbody"])

    # ── 4. scope ──
    print("== 4. scope guards ==")
    template = TEMPLATE.read_text(encoding="utf-8")
    hooks_in_template = template.count('class="reward-role"')
    check("Prestige, bonus-role and blacklist cells were not touched (Level Rewards only)",
          hooks_in_template == 2   # the Jinja row + the loadRewards() row
          and '<td><code>${r.role_id}</code></td>' in template,
          f"hooks={hooks_in_template}")
    check("name resolution lives in the page script, not in the shared picker module",
          "reward-role" not in NERO_SELECT.read_text(encoding="utf-8")
          and "applyRoleNames" not in NERO_SELECT.read_text(encoding="utf-8")
          and "return { initAll, initMulti, getMultiValues, isReady };"
          in NERO_SELECT.read_text(encoding="utf-8"))
    css = MAIN_CSS.read_text(encoding="utf-8")
    check("only pre-existing styling is reused - no CSS was added for this",
          ".badge-danger" in css and ".badge-warning" in css and "td.empty" in css
          and ".role-name" not in css)
    check("no new endpoint, storage column or migration was introduced",
          "guild/roles" not in (ROOT / "dashboard" / "api" / "leveling.py").read_text(
              encoding="utf-8")
          and "role_name" not in (ROOT / "dashboard" / "api" / "leveling.py").read_text(
              encoding="utf-8"))
    for path, label in ((COGS_LEVELING, "cogs/leveling.py"),
                        (LEVEL_CLAIMS, "utils/level_claims.py")):
        text = path.read_text(encoding="utf-8")
        check(f"/level rendering and the claim engine know nothing about this ({label})",
              "guild/roles" not in text and "reward-role" not in text
              and "applyRoleNames" not in text and "renderRoleCell" not in text
              and "Name unavailable" not in text)
    # The claim engine has its own "Role not found" failure for a deleted role;
    # U2 borrows the wording on purpose and must not touch the engine itself.
    claims_text = LEVEL_CLAIMS.read_text(encoding="utf-8")
    check("the claim engine's own deleted-role failure is untouched (and the wording agrees)",
          'raise RuntimeError("Role not found")' in claims_text)
    check("the U1 in-place refresh, audience filter and delete path are intact",
          "loadRewards();" in template
          and "location.reload" not in template
          and "function applyRewardFilter()" in template
          and "showConfirm('Delete Level ${r.level} reward?', () => deleteReward(${r.id}))"
          in template)
    check("role names are display-only: nothing writes to the reward tables",
          "INSERT" not in template and "UPDATE leveling_rewards" not in template)

    print("ALL U2 ROLE-NAME CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1)
