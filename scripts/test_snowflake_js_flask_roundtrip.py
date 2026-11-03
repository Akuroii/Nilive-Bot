"""End-to-end JavaScript (Node VM) + Flask round-trip tests for 64-bit Discord snowflakes.

Covers:
1. Exact production guild snowflakes (1405993421167919336, 1360461358486913145,
   1280983591148584971, 716855999746670673) and user snowflake (704453350384730237)
   through Flask JSON -> real Node JSON.parse + rendered template JS -> Flask routes.
2. All 8 server-selection, category, developer-bypass, CSRF, stale-tab, and
   Member Blacklist 64-bit snowflake round-trip scenarios.
"""
import json
from pathlib import Path
import re
import subprocess
import time
import unittest
from unittest.mock import patch

import phase1_support as S
import dashboard.app as dashboard
from dashboard.auth import BotMembershipStatus

PROD_GUILDS = (
    1405993421167919336,  # JS float64 rounds to 1405993421167919400
    1360461358486913145,  # JS float64 rounds to 1360461358486913000
    1280983591148584971,  # JS float64 rounds to 1280983591148585000
    716855999746670673,   # JS float64 rounds to 716855999746670700
)
DEV_USER_ID = 704453350384730237      # JS float64 rounds to 704453350384730200
NORMAL_USER_ID = 880000000000000019   # JS float64 rounds to 880000000000000000
UNINSTALLED_GUILD = 1405993421167919999


def _run_node(js_source: str, payload: dict) -> dict:
    proc = subprocess.run(
        ["node", "-e", js_source],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        cwd=str(S.ROOT),
    )
    return json.loads(proc.stdout)


def _eval_server_select_in_node(page_html: str, raw_api_json: str, csrf_token: str) -> dict:
    """Run the actual rendered server_select.html inline <script> inside Node vm."""
    accessible_match = re.search(
        r'<script id="accessible-server-data" type="application/json">([\s\S]*?)</script>',
        page_html,
    )
    assert accessible_match, "Missing #accessible-server-data script block in /server-select HTML"
    script_blocks = re.findall(r"<script>([\s\S]*?)</script>", page_html)
    picker_js = next(b for b in script_blocks if "function loadUnifiedServers()" in b)

    harness = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

const parsedRaw = JSON.parse(input.rawApiJson);
const floatRoundedIds = (parsedRaw.servers || []).map(s => String(s.id));
const exactIdStrs = (parsedRaw.servers || []).map(s => s.id_str);

const mount = { innerHTML: '' };
const dataEl = { textContent: input.accessibleJson };
const submittedForms = [];
const fetchCalls = [];
const openedUrls = [];

const document = {
  getElementById(id) {
    if (id === 'unified-servers-mount') return mount;
    if (id === 'accessible-server-data') return dataEl;
    return null;
  },
  createElement(tag) {
    return {
      tagName: tag.toUpperCase(),
      children: [],
      appendChild(child) { this.children.push(child); },
      submit() {
        const fields = {};
        for (const c of this.children) fields[c.name] = c.value;
        submittedForms.push({ method: this.method, action: this.action, fields });
      },
    };
  },
  body: { appendChild() {} },
};

const windowObj = {
  __CSRF_TOKEN__: input.csrfToken,
  open(url) { openedUrls.push(url); },
};

const ctx = {
  window: windowObj,
  document,
  console,
  fetch: async (url) => {
    fetchCalls.push(url);
    if (url === '/api/user/servers') {
      return { ok: true, status: 200, json: async () => JSON.parse(input.rawApiJson) };
    }
    if (url.startsWith('/api/invite-bot/')) {
      const gid = url.slice('/api/invite-bot/'.length);
      return { ok: true, status: 200, json: async () => ({ url: 'https://discord.com/oauth2/authorize?guild_id=' + gid }) };
    }
    throw new Error('Unexpected fetch: ' + url);
  },
};

vm.runInNewContext(input.pickerJs, ctx);

(async () => {
  await new Promise(r => setImmediate(r));
  const html = mount.innerHTML;
  const onclicks = [...html.matchAll(/onclick="([^"]+)"/g)].map(m => m[1]);
  for (const expr of onclicks) {
    const fn = expr.replace(/,\s*this\)/, ', {disabled:false, textContent:"Invite"})');
    await vm.runInNewContext(fn, Object.assign(ctx, windowObj));
  }
  process.stdout.write(JSON.stringify({
    html,
    floatRoundedIds,
    exactIdStrs,
    onclicks,
    submittedForms,
    fetchCalls,
    openedUrls,
  }));
})().catch(err => { console.error(err); process.exit(1); });
"""
    return _run_node(
        harness,
        {
            "accessibleJson": accessible_match.group(1),
            "rawApiJson": raw_api_json,
            "pickerJs": picker_js,
            "csrfToken": csrf_token,
        },
    )


class SnowflakeJsFlaskRoundTripTests(unittest.TestCase):
    def setUp(self):
        S.execute("DELETE FROM dashboard_users")
        S.execute("DELETE FROM leveling_member_blacklist")
        self.client = dashboard.app.test_client()

    def _login(self, user_id: int, csrf_token: str = "rt-csrf-token", guild_id: int | None = None):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["user"] = {"id": str(user_id), "username": "SnowflakeTester", "avatar": None}
            sess["access_token"] = "oauth-test-token"
            sess["expires_at"] = time.time() + 3600
            sess["csrf_token"] = csrf_token
            if guild_id is not None:
                sess["guild_id"] = guild_id
                sess["user_level"] = "owner"
                sess["guild_name"] = f"Guild {guild_id}"

    def test_01_normal_user_four_categories_js_to_flask_roundtrip(self):
        """Scenario 1, 4, 5, 6: Active, Invite, Access Required, and Dangling with real 64-bit snowflakes."""
        g_active, g_invite, g_access_req, g_dangling = PROD_GUILDS
        # Grant DB access on g_active and g_dangling only
        S.execute(
            "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES "
            "(?, ?, 'admin', 1), (?, ?, 'owner', 1)",
            (g_active, NORMAL_USER_ID, g_dangling, NORMAL_USER_ID),
        )
        oauth_guilds = [
            {"id": str(g_active), "name": "Prod Active", "permissions": "0", "icon": "a1b2c3d4", "owner": False},
            {"id": str(g_invite), "name": "Prod Invite", "permissions": "32", "icon": None, "owner": False},
            {"id": str(g_access_req), "name": "Prod AccessReq", "permissions": "8", "icon": None, "owner": False},
            {"id": str(g_dangling), "name": "Prod Dangling", "permissions": "32", "icon": None, "owner": True},
        ]
        bot_guilds = [
            {"id": str(g_active), "name": "Prod Active", "icon": "a1b2c3d4"},
            {"id": str(g_access_req), "name": "Prod AccessReq", "icon": None},
        ]

        self._login(NORMAL_USER_ID)
        with patch("dashboard.app.fetch_discord_guilds", return_value=oauth_guilds), \
             patch("dashboard.auth.fetch_bot_guilds_full", return_value=bot_guilds):
            page_res = self.client.get("/server-select")
            self.assertEqual(page_res.status_code, 200)
            api_res = self.client.get("/api/user/servers")
            self.assertEqual(api_res.status_code, 200)
            raw_api_json = api_res.get_data(as_text=True)

            js_out = _eval_server_select_in_node(
                page_res.get_data(as_text=True), raw_api_json, "rt-csrf-token"
            )

            # Confirm IEEE-754 float64 WOULD corrupt all 4 numeric IDs in JS, while id_str stays exact
            self.assertNotEqual(set(js_out["floatRoundedIds"]), {str(g) for g in PROD_GUILDS})
            self.assertEqual(set(js_out["exactIdStrs"]), {str(g) for g in PROD_GUILDS})

            # Verify all 4 categories rendered with exact unrounded snowflake handlers
            self.assertIn(f"selectServerPost('{g_active}')", js_out["onclicks"])
            self.assertIn(f"inviteBot('{g_invite}', this)", js_out["onclicks"])
            self.assertIn(f"inviteBot('{g_dangling}', this)", js_out["onclicks"])
            self.assertIn(f"selectServerPost('{g_dangling}')", js_out["onclicks"])
            # access_required server must NOT get a selectServerPost or inviteBot action
            for expr in js_out["onclicks"]:
                self.assertNotIn(str(g_access_req), expr)

            # Submit the exact form produced by JS selectServerPost for g_active -> Flask 302 -> /
            active_form = next(f for f in js_out["submittedForms"] if str(g_active) in f["action"])
            self.assertEqual(active_form["action"], f"/select-guild/{g_active}")
            post_res = self.client.post(active_form["action"], data=active_form["fields"])
            self.assertEqual(post_res.status_code, 302)
            self.assertEqual(post_res.headers["Location"], "/")
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["guild_id"], g_active)
                self.assertEqual(sess["user_level"], "admin")

            # Verify invite endpoint works with exact snowflake while select-guild 403s for invite & access_required
            inv_res = self.client.get(f"/api/invite-bot/{g_invite}")
            self.assertEqual(inv_res.status_code, 200)
            self.assertIn(f"guild_id={g_invite}", inv_res.get_json()["url"])
            self.assertEqual(
                self.client.post(f"/select-guild/{g_invite}", data={"csrf_token": "rt-csrf-token"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(f"/select-guild/{g_access_req}", data={"csrf_token": "rt-csrf-token"}).status_code,
                403,
            )

            # Verify dangling server Review Logs form produced by JS -> 302
            dangling_form = next(f for f in js_out["submittedForms"] if str(g_dangling) in f["action"])
            dangling_res = self.client.post(dangling_form["action"], data=dangling_form["fields"])
            self.assertEqual(dangling_res.status_code, 302)
            with self.client.session_transaction() as sess:
                self.assertEqual(sess["guild_id"], g_dangling)
                self.assertEqual(sess["user_level"], "owner")

    def test_02_developer_bypass_all_four_production_guilds_and_nonmember_denial(self):
        """Scenario 2 & 3: Trusted super-admin (704453350384730237) selects all 4 bot guilds without DB rows, 403s on non-bot guild."""
        bot_guilds = [{"id": str(gid), "name": f"Prod {i}", "icon": None} for i, gid in enumerate(PROD_GUILDS)]
        self._login(DEV_USER_ID)

        with patch.object(dashboard, "is_trusted_super_admin", side_effect=lambda uid: int(uid) == DEV_USER_ID), \
             patch("dashboard.permissions.is_trusted_super_admin", side_effect=lambda uid: int(uid) == DEV_USER_ID), \
             patch("dashboard.app.fetch_discord_guilds", return_value=[]), \
             patch("dashboard.auth.fetch_bot_guilds_full", return_value=bot_guilds), \
             patch(
                 "dashboard.auth.check_bot_guild_membership",
                 side_effect=lambda gid: (
                     BotMembershipStatus.INSTALLED if int(gid) in PROD_GUILDS else BotMembershipStatus.CONFIRMED_NO_ACCESS
                 ),
             ):
            page_res = self.client.get("/server-select")
            api_res = self.client.get("/api/user/servers")
            js_out = _eval_server_select_in_node(
                page_res.get_data(as_text=True), api_res.get_data(as_text=True), "rt-csrf-token"
            )
            self.assertEqual(len(js_out["submittedForms"]), 4)
            for gid in PROD_GUILDS:
                form = next(f for f in js_out["submittedForms"] if f["action"] == f"/select-guild/{gid}")
                res = self.client.post(form["action"], data=form["fields"])
                self.assertEqual(res.status_code, 302)
                with self.client.session_transaction() as sess:
                    self.assertEqual(sess["guild_id"], gid)
                    self.assertEqual(sess["user_level"], "owner")

            # Developer bypass must NEVER write to dashboard_users
            self.assertEqual(S.rows("SELECT * FROM dashboard_users WHERE user_id = ?", (DEV_USER_ID,)), [])

            # Non-bot guild must still 403 even for super-admin
            res_uninstalled = self.client.post(
                f"/select-guild/{UNINSTALLED_GUILD}",
                data={"csrf_token": "rt-csrf-token"},
            )
            self.assertEqual(res_uninstalled.status_code, 403)

    def test_03_csrf_get_confirmation_and_stale_tab_page_permission_check(self):
        """Scenario 7: GET /select-guild/<id> confirms without switching; ?g=<id> enforces target page permission."""
        g_primary, g_secondary = PROD_GUILDS[0], PROD_GUILDS[1]
        # Primary guild has 'admin', secondary guild only has 'rank' (cannot access /moderation)
        S.execute(
            "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES "
            "(?, ?, 'admin', 1), (?, ?, 'rank', 1)",
            (g_primary, NORMAL_USER_ID, g_secondary, NORMAL_USER_ID),
        )
        self._login(NORMAL_USER_ID, guild_id=g_primary)

        # GET /select-guild/<snowflake> renders confirmation and does NOT switch session
        get_res = self.client.get(f"/select-guild/{g_secondary}")
        self.assertEqual(get_res.status_code, 200)
        self.assertIn("Confirm Server Selection", get_res.get_data(as_text=True))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["guild_id"], g_primary)

        # Stale-tab GET /moderation?g=<g_secondary> must 403 because 'rank' cannot access moderation_view
        stale_denied = self.client.get(f"/moderation?g={g_secondary}")
        self.assertEqual(stale_denied.status_code, 403)

        # Upgrade g_secondary to 'moderator' -> stale-tab GET /moderation?g=<g_secondary> renders mismatch prompt
        S.execute(
            "UPDATE dashboard_users SET permission_level = 'moderator' WHERE guild_id = ? AND user_id = ?",
            (g_secondary, NORMAL_USER_ID),
        )
        stale_ok = self.client.get(f"/moderation?tab=warnings&g={g_secondary}")
        self.assertEqual(stale_ok.status_code, 200)
        html = stale_ok.get_data(as_text=True)
        self.assertIn("Active Server Mismatch", html)
        self.assertIn(f"/select-guild/{g_secondary}", html)
        self.assertIn("/moderation?tab=warnings", html)

    def test_04_member_blacklist_64bit_user_id_js_to_flask_roundtrip(self):
        """Scenario 8: Member Blacklist 64-bit user_id (704453350384730237) survives JS render -> retry -> unblacklist."""
        g_active = PROD_GUILDS[0]
        S.execute(
            "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) VALUES (?, ?, 'owner', 1)",
            (g_active, NORMAL_USER_ID),
        )
        self._login(NORMAL_USER_ID, guild_id=g_active)
        headers = {"X-CSRF-Token": "rt-csrf-token", "X-Guild-ID": str(g_active)}

        # 1. Add 64-bit user snowflake to blacklist
        add_res = self.client.post(
            "/api/leveling/member-blacklist",
            json={"user_id": str(DEV_USER_ID), "reason": "Snowflake roundtrip"},
            headers=headers,
        )
        self.assertEqual(add_res.status_code, 200)

        # 2. Fetch raw JSON bytes from GET /api/leveling/member-blacklist
        list_res = self.client.get("/api/leveling/member-blacklist", headers=headers)
        self.assertEqual(list_res.status_code, 200)
        raw_json = list_res.get_data(as_text=True)

        # 3. Extract actual Member Blacklist JS functions from leveling.html and run in Node VM
        leveling_tpl = (Path(S.ROOT) / "dashboard/templates/systems/leveling.html").read_text(encoding="utf-8")
        start = leveling_tpl.index("// ── MEMBER BLACKLIST (Phase D3)")
        end = leveling_tpl.index("// ── PRESTIGE (finalized Prestige system)")
        blacklist_js = leveling_tpl[start:end]

        node_driver = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const rawParsed = JSON.parse(input.rawJson);
const floatRoundedUid = String(rawParsed.members[0].user_id);

const listEl = { innerHTML: '' };
const requests = [];
const ctx = {
  document: { getElementById: id => id === 'member-blacklist-list' ? listEl : null },
  escapeHtml: s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
  showToast() {},
  fetch: async (url, init) => {
    requests.push({ url, method: (init && init.method) || 'GET', body: init && init.body ? JSON.parse(init.body) : null });
    if (url === '/api/leveling/member-blacklist') {
      return { ok: true, json: async () => JSON.parse(input.rawJson) };
    }
    return { ok: true, json: async () => ({ success: true }) };
  },
};
vm.runInNewContext(input.blacklistJs, ctx);
(async () => {
  await ctx.loadMemberBlacklist();
  const renderedHtml = listEl.innerHTML;
  const onclicks = [...renderedHtml.matchAll(/onclick="([^"]+)"/g)].map(m => m[1]);
  for (const expr of onclicks) {
    await vm.runInNewContext(expr, ctx);
  }
  process.stdout.write(JSON.stringify({ floatRoundedUid, renderedHtml, onclicks, requests }));
})().catch(e => { console.error(e); process.exit(1); });
"""
        out = _run_node(node_driver, {"rawJson": raw_json, "blacklistJs": blacklist_js})

        # Confirm float64 rounded 704453350384730237 -> 704453350384730200, while UI rendered exact 704453350384730237
        self.assertEqual(out["floatRoundedUid"], "704453350384730200")
        self.assertIn(f"<code>{DEV_USER_ID}</code>", out["renderedHtml"])
        self.assertNotIn("704453350384730200", out["renderedHtml"])

        retry_req = next(r for r in out["requests"] if r["url"] == "/api/leveling/member-blacklist/retry-roles")
        delete_req = next(r for r in out["requests"] if r["method"] == "DELETE")
        self.assertEqual(retry_req["body"], {"user_id": str(DEV_USER_ID)})
        self.assertEqual(delete_req["url"], f"/api/leveling/member-blacklist/{DEV_USER_ID}")

        # 4. Execute those exact JS-produced requests against Flask!
        r_retry = self.client.post(retry_req["url"], json=retry_req["body"], headers=headers)
        self.assertEqual(r_retry.status_code, 200)

        # Confirm that the old float64-corrupted ID WOULD fail with 404, whereas the exact JS URL succeeds with 200
        r_corrupt = self.client.delete("/api/leveling/member-blacklist/704453350384730200", headers=headers)
        self.assertEqual(r_corrupt.status_code, 404)

        r_del = self.client.delete(delete_req["url"], headers=headers)
        self.assertEqual(r_del.status_code, 200)
        self.assertTrue(r_del.get_json()["success"])


if __name__ == "__main__":
    unittest.main()
