"""U1: adding or deleting a Role reward must not reload the Leveling page.

Before the fix the Rewards tab was the only track without a JSON list endpoint,
so `addReward()` / `deleteReward()` did `setTimeout(() => location.reload(), 600)`.
A reload re-runs the page script, whose init ends with `switchTab('leaderboard')`
and whose filter selects are rebuilt empty - so the admin was thrown back to the
Leaderboard tab with the audience/level filters cleared.

The fix mirrors what the currency / shop / boost tracks already do: a read-only
`GET /api/leveling/rewards` plus a `loadRewards()` that re-renders
`#rewards-tbody` in place and then calls `syncRewardCounts()` and
`applyRewardFilter()`.

What this file proves:

1. the new endpoint is read-only, guild-scoped, ordered like the page route, and
   reflects a POST/DELETE immediately (so the in-place re-render is truthful);
2. the server-rendered first paint is unchanged (same rows, same attributes);
3. BEHAVIOUR, by executing the served page script in node against a stub DOM:
   add and delete never call `location.reload`, never switch the tab, keep the
   active audience filter applied, update the row-count badge, and report
   success/error through the existing toast;
4. scope: no other template lost its reload, the tab default is untouched, the
   existing POST/DELETE routes and `_reward_audience` are byte-identical, and
   exactly one route was added to the API module.

Run with:
    python scripts/test_leveling_rewards_ux_u1.py
"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from phase1_support import GUILD, USER, execute, reset_database, rows

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "dashboard" / "templates" / "systems" / "leveling.html"
API_MODULE = ROOT / "dashboard" / "api" / "leveling.py"
OTHER_GUILD = GUILD + 1
ADMIN_ID = USER + 500
OUTSIDER_ID = USER + 777
CSRF = "u1-csrf"

# Role rewards used across the checks: (level, role_id, audience).
SEED = [(3, 600, "everyone"), (5, 601, "everyone"), (10, 602, "boosters")]
ADDED = {"level": 7, "role_id": "603", "audience": "boosters"}
API_ERROR = "That role already has a reward at this level"


class Fail(Exception):
    pass


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        raise Fail(label)


def page_script_in_window(html: str) -> str:
    """The largest inline script inside #content-area (same window the
    dashboard page-script harness uses)."""
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
            user={"id": user_id, "username": "U1 Tester", "avatar": None},
            guild_id=GUILD, guild_name="Guild", user_level=permission,
            expires_at=time.time() + 7200, csrf_token=CSRF)
    return client


NODE_HARNESS = r"""
const fs = require('fs');
const out = { reloads: 0, toasts: [], confirms: [], fetches: [], errors: [],
              switchTabCalls: 0, rows: null, badge: null, summary: null,
              tabs: null, tbody: null };
const nativeTimeout = setTimeout;
const settle = (ms) => new Promise(r => nativeTimeout(r, ms || 20));
process.on('unhandledRejection', e => out.errors.push('unhandled: ' + ((e && e.message) || e)));

// ── stub DOM ──────────────────────────────────────────────────────────────
const TABS = ['leaderboard', 'config', 'rewards', 'bonuses', 'prestige'];
const TBODIES = ['rewards-tbody', 'currency-rewards-tbody',
                 'boost-rewards-tbody', 'shop-rewards-tbody'];

// Row objects are cached per tbody markup, so applyRewardFilter() and
// updateFilterSummary() mutate and report on the SAME objects the way a real
// DOM does. document.querySelectorAll('tr.reward-row') spans all four reward
// tables (as it does in the page), while a tbody's own querySelectorAll sees
// only its rows - syncRewardCounts() depends on that difference.
let cacheKey = null, cacheRows = [];
function allRewardRows() {
    const key = TBODIES.map(id => (els[id] ? els[id].innerHTML : '')).join('|');
    if (key !== cacheKey) {
        cacheKey = key;
        cacheRows = [];
        TBODIES.forEach(id => {
            const html = els[id] ? els[id].innerHTML : '';
            const re = /<tr class="reward-row"([^>]*)>/g;
            let m;
            while ((m = re.exec(html)) !== null) {
                const dataset = {};
                m[1].replace(/data-([\w-]+)="([^"]*)"/g, (_, k, v) => {
                    dataset[k.replace(/-(\w)/g, (s2, c) => c.toUpperCase())] = v;
                    return '';
                });
                cacheRows.push({ style: {}, dataset, _tbody: id });
            }
        });
    }
    out.rows = cacheRows.filter(r => r._tbody === 'rewards-tbody')
        .map(r => ({ audience: r.dataset.audience, level: r.dataset.level,
                     id: r.dataset.id, display: r.style.display || '' }));
    return cacheRows;
}

function makeEl(id) {
    return { id, value: '', innerHTML: '', textContent: '', style: {}, dataset: {},
             children: [], options: [], disabled: false,
             classList: { contains: () => false, add() {}, remove() {}, toggle() {} },
             querySelectorAll(sel) {
                 return sel === 'tr.reward-row'
                     ? allRewardRows().filter(r => r._tbody === id) : [];
             },
             querySelector: () => null,
             addEventListener() {}, appendChild() {}, insertAdjacentHTML() {},
             focus() {}, click() {}, remove() {},
             getAttribute: () => null, setAttribute() {} };
}
const els = {};
function el(id) { return els[id] || (els[id] = makeEl(id)); }

global.document = {
    getElementById: el,
    querySelectorAll(sel) {
        if (sel === 'tr.reward-row') return allRewardRows();
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
global.setTimeout = (fn, ms) => { try { fn(); } catch (e) { out.errors.push('timeout: ' + e.message); } return 0; };
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

// ── stub API: the role-reward endpoints behave like the real ones ─────────
const state = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const reply = payload => Promise.resolve({ ok: true, status: 200,
                                             json: () => Promise.resolve(payload) });
global.fetch = function (url, opts) {
    const method = (opts && opts.method) || 'GET';
    out.fetches.push(method + ' ' + url);
    if (url === '/api/leveling/rewards' && method === 'GET') {
        return reply({ rewards: state.rewards });
    }
    if (url === '/api/leveling/reward' && method === 'POST') {
        if (state.failNext) return reply({ success: false, error: state.failError });
        const body = JSON.parse(opts.body);
        state.rewards.push({ id: ++state.nextId, level: body.level,
                             role_id: body.role_id,
                             audience: body.audience || 'everyone' });
        state.rewards.sort((a, b) => a.level - b.level);
        return reply({ success: true });
    }
    const gone = url.match(/^\/api\/leveling\/reward\/(\d+)$/);
    if (gone && method === 'DELETE') {
        state.rewards = state.rewards.filter(r => String(r.id) !== gone[1]);
        return reply({ success: true });
    }
    return reply({});
};

(async function main() {
    const src = fs.readFileSync(process.argv[2], 'utf8');
    // The page script's own init ends with switchTab('leaderboard') - that is
    // the pre-existing default on every load, and exactly why a reload used to
    // lose the tab. So evaluate first, then have the admin click the Rewards
    // tab and pick the Booster filter, as they would in the browser.
    try { (0, eval)(src); } catch (e) { out.errors.push('init: ' + e.message); }
    await settle(30);
    out.tabAfterInit = { rewards: el('tab-content-rewards').style.display,
                         leaderboard: el('tab-content-leaderboard').style.display };
    global.switchTab('rewards');
    el('reward-filter-audience').value = 'boosters';
    el('reward-filter-level').value = '';
    out.tabAfterClick = { rewards: el('tab-content-rewards').style.display,
                          leaderboard: el('tab-content-leaderboard').style.display };

    const realSwitchTab = global.switchTab;
    global.switchTab = function (name) { out.switchTabCalls += 1; return realSwitchTab(name); };
    out.toasts.length = 0;

    // ── add a Booster reward at level 7 while filtered to Booster rewards ──
    el('reward-level').value = String(ADDED_LEVEL);
    el('reward-role-picker').value = ADDED_ROLE;
    el('reward-audience').value = 'boosters';
    await global.addReward();
    await settle(30);
    out.tbody = els['rewards-tbody'].innerHTML;
    out.badge = els['count-roles'].textContent;
    out.summary = els['reward-filter-summary'].textContent;
    out.tabs = { rewards: els['tab-content-rewards'].style.display,
                 leaderboard: els['tab-content-leaderboard'].style.display };
    out.afterAdd = { reloads: out.reloads, toasts: out.toasts.slice(),
                     switchTabCalls: out.switchTabCalls, rows: out.rows,
                     form: { level: els['reward-level'].value,
                             role: els['reward-role-picker'].value } };

    // ── a rejected add must report the API error and change nothing ───────
    // (a successful add clears the form, so fill it in again first: this has to
    // reach the API, not the client-side "Select a level and role" guard)
    el('reward-level').value = String(ADDED_LEVEL);
    el('reward-role-picker').value = ADDED_ROLE;
    el('reward-audience').value = 'boosters';
    state.failNext = true;
    out.toasts.length = 0;
    const before = els['rewards-tbody'].innerHTML;
    await global.addReward();
    await settle(30);
    out.afterFailedAdd = { reloads: out.reloads, toasts: out.toasts.slice(),
                           unchanged: before === els['rewards-tbody'].innerHTML };
    state.failNext = false;

    // ── delete the row that was just added ────────────────────────────────
    out.toasts.length = 0;
    const added = state.rewards.find(r => r.level === ADDED_LEVEL);
    await global.deleteReward(added.id);
    await settle(30);
    out.afterDelete = { reloads: out.reloads, toasts: out.toasts.slice(),
                        switchTabCalls: out.switchTabCalls, rows: out.rows,
                        tbody: els['rewards-tbody'].innerHTML,
                        badge: els['count-roles'].textContent,
                        summary: els['reward-filter-summary'].textContent,
                        tabs: { rewards: els['tab-content-rewards'].style.display,
                                leaderboard: els['tab-content-leaderboard'].style.display } };

    // ── the last reward disappearing must render the empty state ──────────
    state.rewards = [];
    await global.loadRewards();
    await settle(30);
    out.emptyState = els['rewards-tbody'].innerHTML;
    out.emptyBadge = els['count-roles'].textContent;

    out.errors = out.errors.filter(e => /loadRewards|addReward|deleteReward|applyRewardFilter|syncRewardCounts|reward-row/.test(e));
    console.log(JSON.stringify(out));
})().catch(e => { out.errors.push('harness: ' + e.message); console.log(JSON.stringify(out)); });
"""


def run_node(script_text: str, state: dict) -> dict:
    node = shutil.which("node")
    if not node:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "page.js"
        js.write_text(script_text, encoding="utf-8")
        seed = Path(tmp) / "state.json"
        seed.write_text(json.dumps(state), encoding="utf-8")
        harness = Path(tmp) / "harness.js"
        body = (NODE_HARNESS
                .replace("ADDED_LEVEL", str(ADDED["level"]))
                .replace("ADDED_ROLE", json.dumps(ADDED["role_id"])))
        harness.write_text(body, encoding="utf-8")
        result = subprocess.run([node, str(harness), str(js), str(seed)],
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise Fail(f"node harness exited {result.returncode}: "
                       f"{(result.stderr or '').strip()[:400]}")
        return json.loads(result.stdout.strip().splitlines()[-1])


async def main():
    await reset_database()
    from utils.level_claims import ensure_tables

    await ensure_tables()
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    for level, role_id, audience in SEED:
        execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
                "VALUES (?,?,?,?)", (GUILD, level, role_id, audience))
    execute("INSERT INTO leveling_rewards (guild_id,level,role_id,audience) "
            "VALUES (?,?,?,?)", (OTHER_GUILD, 99, 999, "everyone"))

    client = dashboard_client()

    # ── 1. the read endpoint the in-place refresh depends on ──
    print("== 1. GET /api/leveling/rewards ==")
    res = client.get("/api/leveling/rewards")
    check("the endpoint exists and is permitted for a level admin",
          res.status_code == 200, f"status={res.status_code}")
    payload = res.get_json()
    listed = payload["rewards"]
    check("it returns the guild's role rewards, ordered by level like the page route",
          [r["level"] for r in listed] == sorted(r["level"] for r in listed)
          and [r["level"] for r in listed] == [3, 5, 10], str(listed))
    check("each row carries exactly the fields the row markup needs",
          all(set(r) == {"id", "level", "role_id", "audience"} for r in listed),
          str(sorted(listed[0])))
    check("the audience survives, so the Booster badge and filter still work",
          [(r["level"], r["audience"]) for r in listed]
          == [(3, "everyone"), (5, "everyone"), (10, "boosters")], str(listed))
    check("another guild's rewards are never returned",
          all(r["level"] != 99 for r in listed), str(listed))
    check("it is read-only: no row was created, changed or removed",
          rows("SELECT COUNT(*) FROM leveling_rewards", ())[0][0] == len(SEED) + 1)

    res = client.post("/api/leveling/reward", json=ADDED,
                      headers={"X-CSRF-Token": CSRF})
    check("the existing POST contract is unchanged", res.get_json() == {"success": True},
          str(res.get_json()))
    listed = client.get("/api/leveling/rewards").get_json()["rewards"]
    added_id = next(r["id"] for r in listed if r["level"] == ADDED["level"])
    check("a newly added reward is visible to the next read (the refresh is truthful)",
          any(r["level"] == ADDED["level"] and str(r["role_id"]) == ADDED["role_id"]
              and r["audience"] == ADDED["audience"] for r in listed), str(listed))
    res = client.delete(f"/api/leveling/reward/{added_id}",
                        headers={"X-CSRF-Token": CSRF})
    check("the existing DELETE contract is unchanged", res.get_json() == {"success": True},
          str(res.get_json()))
    listed = client.get("/api/leveling/rewards").get_json()["rewards"]
    check("a deleted reward is gone from the next read",
          all(r["level"] != ADDED["level"] for r in listed), str(listed))

    outsider = dashboard_client(user_id=OUTSIDER_ID, permission="admin")
    execute("DELETE FROM dashboard_users WHERE guild_id=? AND user_id=?",
            (GUILD, OUTSIDER_ID))
    res = outsider.get("/api/leveling/rewards")
    check("the new endpoint is behind the same permission gate as its siblings",
          res.status_code == 403 and res.get_json()["success"] is False,
          f"status={res.status_code} body={res.get_json()}")

    # ── 2. the server-rendered first paint is unchanged ──
    print("== 2. server-rendered page ==")
    page = client.get("/leveling")
    html = page.get_data(as_text=True)
    check("the page still renders", page.status_code == 200, f"status={page.status_code}")
    check("the Jinja rows still carry the filter attributes",
          'data-audience="boosters"' in html and 'data-audience="everyone"' in html
          and html.count('class="reward-row"') >= len(SEED))
    check("the Jinja row now carries the same data-id the JS renderer emits",
          re.search(r'<tr class="reward-row" data-id="\d+" data-level="10" '
                    r'data-audience="boosters">', html) is not None)
    check("the delete confirmation copy is unchanged",
          "showConfirm('Delete Level 10 reward?'" in html)

    script = page_script_in_window(html)
    check("the page script is still emitted once inside #content-area",
          script and html.count(script) == 1
          and html.index(script) > html.index('id="content-area"'))

    # ── 3. behaviour: run the served page script against a stub DOM ──
    print("== 3. add / delete without a page reload ==")
    state = {"nextId": 100, "failNext": False, "failError": API_ERROR,
             "rewards": [{"id": i + 1, "level": lv, "role_id": str(rid),
                          "audience": aud}
                         for i, (lv, rid, aud) in enumerate(SEED)]}
    result = run_node(script, state)
    if result is None:
        print("  SKIPPED: node is not installed - the behavioural checks need it")
    else:
        check("the role-reward path ran clean in the stub DOM",
              result["errors"] == [], str(result["errors"]))
        added = result["afterAdd"]
        check("adding a reward never reloads the page", added["reloads"] == 0,
              f"reloads={added['reloads']}")
        check("the page still defaults to Leaderboard on a fresh load (unchanged)",
              result["tabAfterInit"] == {"rewards": "none", "leaderboard": "block"},
              str(result["tabAfterInit"]))
        check("clicking the Rewards tab is what puts the admin there",
              result["tabAfterClick"] == {"rewards": "block", "leaderboard": "none"},
              str(result["tabAfterClick"]))
        check("adding a reward never switches the tab",
              added["switchTabCalls"] == 0
              and result["tabs"] == {"rewards": "block", "leaderboard": "none"},
              str(result["tabs"]))
        check("the new row is in the table in place, with its Booster badge",
              f'Level {ADDED["level"]}' in result["tbody"]
              and ADDED["role_id"] in result["tbody"]
              and "badge badge-booster" in result["tbody"]
              and f'deleteReward(' in result["tbody"], result["tbody"][-320:])
        check("the row-count badge follows the table",
              str(added["rows"].__len__()) == str(result["badge"]) == str(len(SEED) + 1),
              f"badge={result['badge']} rows={len(added['rows'])}")
        check("the active Booster filter is re-applied to the refreshed rows",
              [r["display"] for r in added["rows"] if r["audience"] == "boosters"]
              == ["", ""]
              and all(r["display"] == "none" for r in added["rows"]
                      if r["audience"] == "everyone"),
              str(added["rows"]))
        check("the filter summary still reports the filtered count",
              "2 of 4 reward row(s) match Booster Rewards" == result["summary"],
              result["summary"])
        check("success feedback is the existing toast",
              added["toasts"] == [{"msg": "Reward added!", "type": "success"}],
              str(added["toasts"]))
        check("the refresh reads the list endpoint, not a page reload",
              "POST /api/leveling/reward" in result["fetches"]
              and "GET /api/leveling/rewards" in result["fetches"],
              str([f for f in result["fetches"] if "/reward" in f][-3:]))
        check("the add form is cleared after a successful add (unchanged behaviour)",
              added["form"] == {"level": "", "role": ""}, str(added["form"]))

        failed = result["afterFailedAdd"]
        check("a rejected add reports the API's own error text",
              failed["toasts"] == [{"msg": API_ERROR, "type": "error"}],
              str(failed["toasts"]))
        check("a rejected add changes nothing and does not reload",
              failed["unchanged"] is True and failed["reloads"] == 0, str(failed))

        deleted = result["afterDelete"]
        check("deleting a reward never reloads the page or switches the tab",
              deleted["reloads"] == 0 and deleted["switchTabCalls"] == 0
              and deleted["tabs"]["rewards"] == "block", str(deleted["tabs"]))
        check("the deleted row is gone in place",
              f'Level {ADDED["level"]}' not in deleted["tbody"]
              and len(deleted["rows"]) == len(SEED), str(deleted["rows"]))
        check("deleting keeps the filter applied and the badge in sync",
              str(deleted["badge"]) == str(len(SEED))
              and all(r["display"] == "none" for r in deleted["rows"]
                      if r["audience"] == "everyone"), str(deleted["rows"]))
        check("delete feedback is the existing toast",
              deleted["toasts"] == [{"msg": "Reward deleted", "type": "info"}],
              str(deleted["toasts"]))
        check("the last reward leaving renders the empty state",
              "No role rewards configured yet" in result["emptyState"]
              and 'colspan="3"' in result["emptyState"]
              and str(result["emptyBadge"]) == "0", result["emptyState"])

    # ── 4. scope: nothing else moved ──
    print("== 4. scope guards ==")
    template = TEMPLATE.read_text(encoding="utf-8")
    api_text = API_MODULE.read_text(encoding="utf-8")
    check("no full-page reload is left anywhere in the Leveling template",
          "location.reload" not in template)
    check("the tab default is untouched (the fix removes the reload, not the default)",
          "switchTab('leaderboard');" in template)
    check("exactly one route was added to the leveling API module",
          api_text.count('@api_bp.route("/leveling/rewards", methods=["GET"])') == 1
          and len(re.findall(r'@api_bp\.route\("/leveling/', api_text))
          == 1 + len(re.findall(r'@api_bp\.route\("/leveling/',
                                subprocess.run(
                                    ["git", "show", "HEAD:dashboard/api/leveling.py"],
                                    capture_output=True, text=True,
                                    cwd=ROOT).stdout)))
    check("the existing role POST/DELETE handlers and the audience validator are intact",
          '@api_bp.route("/leveling/reward", methods=["POST"])' in api_text
          and '@api_bp.route("/leveling/reward/<int:reward_id>", methods=["DELETE"])'
          in api_text
          and 'INSERT INTO leveling_rewards (guild_id, level, role_id, audience)'
          in api_text
          and 'DELETE FROM leveling_rewards WHERE id=? AND guild_id=?' in api_text
          and """    return audience if audience in ("everyone", "boosters") else None"""
          in api_text)
    check("the new endpoint writes nothing",
          not re.search(r'def get_leveling_rewards\(\):[\s\S]*?@api_bp\.route',
                        api_text).group(0).count("INSERT")
          and "INSERT" not in api_text[api_text.index("def get_leveling_rewards():"):
                                       api_text.index("def _reward_audience(")])
    untouched = ["general/backups.html", "manage/commands.html",
                 "manage/customcommands.html", "manage/moderation.html",
                 "manage/triggers.html", "systems/shop.html"]
    check("templates outside this fix kept their own reloads (no general refactor)",
          all("location.reload" in
              (ROOT / "dashboard" / "templates" / rel).read_text(encoding="utf-8")
              for rel in untouched),
          str([rel for rel in untouched
               if "location.reload" not in
               (ROOT / "dashboard" / "templates" / rel).read_text(encoding="utf-8")]))
    check("the sibling loaders were not touched",
          all(name in template for name in
              ("async function loadCurrencyRewards()", "async function loadShopRewards()",
               "async function loadBoostRewards()", "async function loadBonusRoles()")))
    check("reward storage, entitlements and the claim engine are not referenced here",
          "record_crossing" not in template and "claim_available" not in template
          and "level_reward_claims" not in api_text.split("def get_leveling_rewards():")[1]
          .split("def _reward_audience(")[0])

    print("ALL U1 ROLE-REWARD REFRESH CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Fail as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1)
