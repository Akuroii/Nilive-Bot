#!/usr/bin/env python3
"""
Regression and audit reproduction suite for Nilive-Bot Dashboard stabilization:
- Issue A: Discord Bot identity + guild member lookup (/users/@me -> /guilds/{id}/members/{bot_id}),
  tri-state bot_status (present / absent / unknown), permission & role hierarchy checks,
  no false bot_missing on 401/403/404(ambiguous)/429/5xx/timeout/network/malformed responses,
  and live bot profile lookup in utils/bot_profile.py.
- Issue B: Server discovery pagination + failure preservation (GuildDiscoveryList / degraded state),
  distinguishing genuine empty results from missing token or Discord API failures.
- Issue C: Dark/light native <select>/<option>/<optgroup> CSS & Select2 stylesheet ordering.
- Issue D & E: load_dotenv() import order in dashboard/app.py, SQLite WAL cross-connection visibility,
  and end-to-end Dashboard -> DB -> Bot synchronization (settings, leveling, shop + prepared icon,
  shop-designer preview/save, server context switching).
- Security & Data Integrity: SSRF block in utils/bot_profile.py, HTML form CSRF enforcement,
  /config/access input validation, role-assignability validation on live AJAX routes + pickers,
  and 64-bit Discord snowflake (> 2^53) string serialization across Dashboard JSON APIs.
"""

import ast
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

_TMP_DIR = tempfile.mkdtemp(prefix="nero_audit_")
os.environ["NERO_ENVIRONMENT"] = "staging"
os.environ["DATABASE_PATH"] = os.path.join(_TMP_DIR, "prod_guard.db")
os.environ["STAGING_DATABASE_PATH"] = os.path.join(_TMP_DIR, "audit_staging.db")
os.environ["OWNER_ID"] = "999999999"
os.environ["SECRET_KEY"] = "0123456789abcdef0123456789abcdef"
os.environ["DISCORD_TOKEN"] = "test_secret_bot_token_do_not_leak"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
import requests

from database import DB_PATH, init_db
from dashboard.utils.async_utils import run_async

run_async(init_db())

from dashboard.app import app
from dashboard import auth
from dashboard.utils import role_validation as rv
from utils import bot_profile

SNOWFLAKE_GUILD = 1088248319982006321
SNOWFLAKE_ROLE_A = 1088248319982006323
SNOWFLAKE_ROLE_B = 1088248319982006327
SNOWFLAKE_CHAN_A = 1088248319982006331
SNOWFLAKE_USER_A = 1088248319982006339
BOT_USER_ID = "987654321098765432"


def _mock_resp(status_code=200, json_data=None, json_exc=None, headers=None, content=b""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.content = content
    if json_exc is not None:
        resp.json.side_effect = json_exc
    else:
        resp.json.return_value = json_data
    resp.iter_content = lambda chunk_size=8192: [content] if content else []
    return resp


class TestDashboardStabilizationAudit(unittest.TestCase):
    def setUp(self):
        if hasattr(rv, "clear_bot_identity_cache"):
            rv.clear_bot_identity_cache()
        self.client = app.test_client()
        self.admin_id = 123456789012345678

        async def _seed():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM dashboard_users")
                await db.execute(
                    "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) "
                    "VALUES (?, ?, 'owner', 1)",
                    (SNOWFLAKE_GUILD, self.admin_id),
                )
                await db.commit()

        run_async(_seed())

    def _login(self, guild_id=SNOWFLAKE_GUILD, level="owner", user_id=None):
        with self.client.session_transaction() as sess:
            sess["user"] = {"id": str(user_id or self.admin_id), "username": "AuditOwner", "avatar": None}
            sess["access_token"] = "test_user_oauth_token"
            sess["expires_at"] = time.time() + 3600
            sess["csrf_token"] = "csrf_valid_token_123"
            if guild_id is not None:
                sess["guild_id"] = guild_id
                sess["guild_name"] = "Snowflake Guild"
                sess["user_level"] = level

    # ── 1. ISSUE A: Bot Detection, Identity Lookup & Tri-State Status ──────

    def test_issue_a_uses_bot_user_id_never_members_at_me_and_computes_role_perms(self):
        called_urls = []
        called_headers = []

        sample_roles = [
            {"id": str(SNOWFLAKE_GUILD), "name": "@everyone", "position": 0, "color": 0, "managed": False, "permissions": "0"},
            {"id": str(SNOWFLAKE_ROLE_A), "name": "Rewardable", "position": 2, "color": 0x00FF00, "managed": False, "permissions": "0"},
            {"id": "1088248319982006399", "name": "Nero", "position": 5, "color": 0x8B5CF6, "managed": True, "permissions": str(rv.MANAGE_ROLES_PERM)},
        ]
        # Real Discord GET /guilds/{id}/members/{user_id} omits top-level "permissions"
        member_payload = {"user": {"id": BOT_USER_ID, "username": "Nero"}, "roles": ["1088248319982006399"]}

        def fake_get(url, headers=None, timeout=None, **kwargs):
            called_urls.append(url)
            called_headers.append(headers or {})
            if url.endswith("/users/@me"):
                return _mock_resp(200, {"id": BOT_USER_ID, "username": "Nero"})
            if url.endswith(f"/guilds/{SNOWFLAKE_GUILD}/roles"):
                return _mock_resp(200, sample_roles)
            if url.endswith(f"/guilds/{SNOWFLAKE_GUILD}/members/{BOT_USER_ID}"):
                return _mock_resp(200, member_payload)
            if url.endswith("/members/@me"):
                raise AssertionError("Must NEVER call OAuth-only GET /guilds/{id}/members/@me with Bot token!")
            return _mock_resp(404, {"code": 0})

        with patch("dashboard.utils.role_validation._req.get", side_effect=fake_get):
            roles, bot_member, err = rv.fetch_guild_roles_and_bot_member(SNOWFLAKE_GUILD)
            self.assertIsNone(err)
            self.assertIsNotNone(bot_member)
            info = rv.compute_role_assignability(SNOWFLAKE_GUILD, roles, bot_member)
            self.assertEqual(info.get("bot_status"), "present")
            self.assertTrue(info["has_manage_roles"])
            self.assertTrue(info["role_map"][str(SNOWFLAKE_ROLE_A)]["assignable"])
            for h in called_headers:
                self.assertEqual(h.get("Authorization"), "Bot test_secret_bot_token_do_not_leak")
            self.assertFalse(any("/members/@me" in u for u in called_urls))

    def test_issue_a_tri_state_absent_vs_unknown_across_all_failure_modes(self):
        sample_roles = [
            {"id": str(SNOWFLAKE_GUILD), "name": "@everyone", "position": 0, "color": 0, "managed": False, "permissions": "0"},
            {"id": str(SNOWFLAKE_ROLE_A), "name": "MemberRole", "position": 1, "color": 0, "managed": False, "permissions": "0"},
        ]

        # Confirmed 404 Unknown Member (10007) -> absent -> bot_missing
        def fake_absent(url, **kwargs):
            if url.endswith("/users/@me"):
                return _mock_resp(200, {"id": BOT_USER_ID})
            if url.endswith("/roles"):
                return _mock_resp(200, sample_roles)
            return _mock_resp(404, {"code": 10007, "message": "Unknown Member"})

        with patch("dashboard.utils.role_validation._req.get", side_effect=fake_absent):
            roles, bot_member, err = rv.fetch_guild_roles_and_bot_member(SNOWFLAKE_GUILD)
            info = rv.compute_role_assignability(SNOWFLAKE_GUILD, roles, bot_member)
            self.assertEqual(info.get("bot_status"), "absent")
            self.assertEqual(info["role_map"][str(SNOWFLAKE_ROLE_A)]["unassignable_reason"], "bot_missing")

        # Indeterminate failure modes MUST NEVER become bot_missing ("Nero not found in server")
        failure_cases = [
            ("401_member", lambda u: _mock_resp(401, {"message": "401: Unauthorized"})),
            ("403_member", lambda u: _mock_resp(403, {"code": 50013, "message": "Missing Permissions"})),
            ("404_ambiguous_no_code", lambda u: _mock_resp(404, {"message": "Not Found"})),
            ("429_rate_limit", lambda u: _mock_resp(429, {"retry_after": 1.5})),
            ("503_upstream", lambda u: _mock_resp(503, {"message": "Service Unavailable"})),
            ("timeout", lambda u: (_ for _ in ()).throw(requests.Timeout("timed out"))),
            ("conn_error", lambda u: (_ for _ in ()).throw(requests.ConnectionError("reset"))),
            ("malformed_json", lambda u: _mock_resp(200, json_exc=ValueError("bad json"))),
            ("malformed_shape", lambda u: _mock_resp(200, {"roles": "not-a-list"})),
        ]
        for label, member_responder in failure_cases:
            with self.subTest(case=label):
                if hasattr(rv, "clear_bot_identity_cache"):
                    rv.clear_bot_identity_cache()

                def _dispatch(url, _mr=member_responder, **kwargs):
                    if url.endswith("/users/@me"):
                        return _mock_resp(200, {"id": BOT_USER_ID})
                    if url.endswith("/roles"):
                        return _mock_resp(200, sample_roles)
                    return _mr(url)

                with patch("dashboard.utils.role_validation._req.get", side_effect=_dispatch):
                    roles, bot_member, err = rv.fetch_guild_roles_and_bot_member(SNOWFLAKE_GUILD)
                    info = rv.compute_role_assignability(SNOWFLAKE_GUILD, roles, bot_member)
                    self.assertEqual(info.get("bot_status"), "unknown", f"{label} should be unknown")
                    r_meta = info["role_map"][str(SNOWFLAKE_ROLE_A)]
                    self.assertFalse(r_meta["assignable"])
                    self.assertNotEqual(r_meta["unassignable_reason"], "bot_missing")
                    self.assertNotIn("not found in server", (r_meta.get("unassignable_label") or "").lower())

                    valid, v_err = rv.validate_assignable_role(SNOWFLAKE_GUILD, str(SNOWFLAKE_ROLE_A))
                    self.assertFalse(valid)
                    self.assertNotIn("not found in server", (v_err or "").lower())
                    self.assertNotIn("test_secret_bot_token_do_not_leak", v_err or "")

    def test_issue_a_discord_10004_unknown_guild_is_unknown_never_absent(self):
        sample_roles = [
            {"id": str(SNOWFLAKE_GUILD), "name": "@everyone", "position": 0, "color": 0, "managed": False, "permissions": "0"},
            {"id": str(SNOWFLAKE_ROLE_A), "name": "AssignableCandidate", "position": 1, "color": 0, "managed": False, "permissions": "0"},
        ]

        def fake_10004(url, **kwargs):
            if url.endswith("/users/@me"):
                return _mock_resp(200, {"id": BOT_USER_ID})
            if url.endswith("/roles"):
                return _mock_resp(200, sample_roles)
            return _mock_resp(404, {"code": 10004, "message": "Unknown Guild"})

        rv.clear_bot_identity_cache()
        self._login()
        with patch("dashboard.utils.role_validation._req.get", side_effect=fake_10004):
            member, status, err = rv.fetch_bot_guild_member(SNOWFLAKE_GUILD)
            self.assertIsNone(member)
            self.assertEqual(status, rv.BotMemberStatus.UNKNOWN)
            self.assertNotEqual(status, rv.BotMemberStatus.ABSENT)
            self.assertIn("10004", err or "")

            roles, bot_member, roles_err = rv.fetch_guild_roles_and_bot_member(SNOWFLAKE_GUILD)
            self.assertIsNone(roles_err)
            info = rv.compute_role_assignability(SNOWFLAKE_GUILD, roles, bot_member)
            self.assertEqual(info["bot_status"], "unknown")
            role_meta = info["role_map"][str(SNOWFLAKE_ROLE_A)]
            self.assertFalse(role_meta["assignable"])
            self.assertEqual(role_meta["unassignable_reason"], "bot_status_unknown")
            self.assertNotEqual(role_meta["unassignable_reason"], "bot_missing")
            self.assertNotIn("nero not found in server", (role_meta.get("unassignable_label") or "").lower())

            api_res = self.client.get("/api/guild/roles", headers={"X-Guild-ID": str(SNOWFLAKE_GUILD)})
            self.assertEqual(api_res.status_code, 200)
            api_text = api_res.get_data(as_text=True)
            api_json = api_res.get_json()
            self.assertEqual(api_json.get("bot_status"), "unknown")
            self.assertNotIn("Nero not found in server", api_text)
            for r_out in api_json.get("results", []):
                self.assertNotEqual(r_out.get("reason"), "bot_missing")

    def test_issue_a_bot_profile_live_member_uses_bot_id_not_at_me(self):
        called = []

        def fake_get(url, **kwargs):
            called.append(url)
            if url.endswith("/users/@me"):
                return _mock_resp(200, {"id": BOT_USER_ID, "username": "Nero"})
            if url.endswith(f"/guilds/{SNOWFLAKE_GUILD}/members/{BOT_USER_ID}"):
                return _mock_resp(
                    200,
                    {
                        "nick": "Nero Guild Nick",
                        "avatar": None,
                        "banner": None,
                        "bio": "Custom bio",
                        "user": {"id": BOT_USER_ID, "username": "Nero", "avatar": None},
                    },
                )
            if url.endswith("/members/@me"):
                return _mock_resp(404, {"code": 10007, "message": "Unknown Member"})
            return _mock_resp(404, {})

        with patch("utils.bot_profile.requests.get", side_effect=fake_get), \
             patch("dashboard.utils.role_validation._req.get", side_effect=fake_get):
            live = bot_profile.get_live_bot_member(SNOWFLAKE_GUILD)
            self.assertIsNotNone(live)
            self.assertEqual(live["nick"], "Nero Guild Nick")
            self.assertFalse(any("/members/@me" in u for u in called))

    def test_issue_a_position_zero_custom_role_and_everyone_hierarchy(self):
        older_pos0_id = str(SNOWFLAKE_GUILD + 10)
        bot_pos0_id = str(SNOWFLAKE_GUILD + 20)
        newer_pos0_id = str(SNOWFLAKE_GUILD + 30)
        bot_high_id = str(SNOWFLAKE_GUILD + 40)

        base_roles = [
            {"id": str(SNOWFLAKE_GUILD), "name": "Nilive Community", "position": 0, "color": 0, "managed": False, "permissions": "0"},
            {"id": older_pos0_id, "name": "OlderPos0", "position": 0, "color": 0x10B981, "managed": False, "permissions": "0"},
            {"id": bot_pos0_id, "name": "BotCustomPos0", "position": 0, "color": 0x8B5CF6, "managed": False, "permissions": str(rv.MANAGE_ROLES_PERM)},
            {"id": newer_pos0_id, "name": "NewerPos0", "position": 0, "color": 0xF59E0B, "managed": False, "permissions": "0"},
            {"id": bot_high_id, "name": "NeroHigh", "position": 2, "color": 0x8B5CF6, "managed": True, "permissions": str(rv.MANAGE_ROLES_PERM)},
        ]

        # Case 1: Bot has a higher custom role (position 2) -> all non-@everyone roles at
        # position 0 are assignable, while actual @everyone (id == guild_id) stays unassignable.
        member_high = {"user": {"id": BOT_USER_ID}, "roles": [bot_high_id]}
        info_high = rv.compute_role_assignability(SNOWFLAKE_GUILD, base_roles, member_high)
        self.assertFalse(info_high["role_map"][str(SNOWFLAKE_GUILD)]["assignable"])
        self.assertEqual(info_high["role_map"][str(SNOWFLAKE_GUILD)]["unassignable_reason"], "everyone")
        self.assertTrue(info_high["role_map"][older_pos0_id]["assignable"])
        self.assertIsNone(info_high["role_map"][older_pos0_id]["unassignable_reason"])
        self.assertTrue(info_high["role_map"][newer_pos0_id]["assignable"])

        # Case 2: Same-position tie at position 0 -> smaller (older) role ID is higher in Discord,
        # larger (newer) role ID is lower and assignable; @everyone stays strictly lowest.
        member_pos0 = {"user": {"id": BOT_USER_ID}, "roles": [bot_pos0_id]}
        info_tie = rv.compute_role_assignability(SNOWFLAKE_GUILD, base_roles, member_pos0)
        self.assertFalse(info_tie["role_map"][str(SNOWFLAKE_GUILD)]["assignable"])
        self.assertEqual(info_tie["role_map"][str(SNOWFLAKE_GUILD)]["unassignable_reason"], "everyone")
        self.assertFalse(info_tie["role_map"][older_pos0_id]["assignable"])
        self.assertEqual(info_tie["role_map"][older_pos0_id]["unassignable_reason"], "above_bot")
        self.assertFalse(info_tie["role_map"][bot_pos0_id]["assignable"])
        self.assertEqual(info_tie["role_map"][bot_pos0_id]["unassignable_reason"], "equal_to_bot")
        self.assertTrue(info_tie["role_map"][newer_pos0_id]["assignable"])
        self.assertIsNone(info_tie["role_map"][newer_pos0_id]["unassignable_reason"])

        # Case 3: Bot has ONLY its default @everyone role (granted MANAGE_ROLES on @everyone).
        # Even though @everyone has the smallest snowflake ID (id == guild_id), @everyone is
        # always the lowest role in Discord, so custom roles at position 0 are above @everyone.
        roles_default_perm = [
            dict(r, permissions=str(rv.MANAGE_ROLES_PERM)) if r["id"] == str(SNOWFLAKE_GUILD) else dict(r)
            for r in base_roles
        ]
        member_default_only = {"user": {"id": BOT_USER_ID}, "roles": []}
        info_default = rv.compute_role_assignability(SNOWFLAKE_GUILD, roles_default_perm, member_default_only)
        self.assertEqual(info_default["bot_top_role"]["id"], str(SNOWFLAKE_GUILD))
        self.assertEqual(info_default["bot_top_role"]["name"], "Nilive Community")
        self.assertTrue(info_default["has_manage_roles"])
        self.assertFalse(info_default["role_map"][str(SNOWFLAKE_GUILD)]["assignable"])
        self.assertEqual(info_default["role_map"][str(SNOWFLAKE_GUILD)]["unassignable_reason"], "everyone")
        self.assertFalse(info_default["role_map"][newer_pos0_id]["assignable"])
        self.assertEqual(info_default["role_map"][newer_pos0_id]["unassignable_reason"], "above_bot")
        self.assertEqual(info_default["role_map"][newer_pos0_id]["unassignable_label"], "Above @everyone")
        self.assertNotIn("@@everyone", info_default["role_map"][newer_pos0_id]["unassignable_label"])

        # Case 4: Confirm GET /api/guild/roles returns custom roles at position 0 as assignable
        # without modifying core.py, and validate_assignable_role() accepts them (and always
        # identifies the default role as "@everyone" — never the guild name or "@@everyone").
        rv.clear_bot_identity_cache()
        self._login()

        def fake_default_only_api(url, **kwargs):
            if url.endswith("/users/@me"):
                return _mock_resp(200, {"id": BOT_USER_ID})
            if url.endswith("/roles"):
                return _mock_resp(200, roles_default_perm)
            if url.endswith(f"/members/{BOT_USER_ID}"):
                return _mock_resp(200, member_default_only)
            return _mock_resp(404, {"code": 0})

        with patch("dashboard.utils.role_validation._req.get", side_effect=fake_default_only_api):
            ok_def, err_def = rv.validate_assignable_role(SNOWFLAKE_GUILD, newer_pos0_id)
            self.assertFalse(ok_def)
            self.assertIn("(@everyone)", err_def or "")
            self.assertNotIn("Nilive Community", err_def or "")
            self.assertNotIn("@@everyone", err_def or "")

        def fake_pos0_api(url, **kwargs):
            if url.endswith("/users/@me"):
                return _mock_resp(200, {"id": BOT_USER_ID})
            if url.endswith("/roles"):
                return _mock_resp(200, base_roles)
            if url.endswith(f"/members/{BOT_USER_ID}"):
                return _mock_resp(200, member_high)
            return _mock_resp(404, {"code": 0})

        with patch("dashboard.utils.role_validation._req.get", side_effect=fake_pos0_api):
            ok_custom, err_custom = rv.validate_assignable_role(SNOWFLAKE_GUILD, newer_pos0_id)
            self.assertTrue(ok_custom, err_custom)
            ok_every, err_every = rv.validate_assignable_role(SNOWFLAKE_GUILD, str(SNOWFLAKE_GUILD))
            self.assertFalse(ok_every)
            self.assertIn("@everyone", err_every or "")

            api_res = self.client.get("/api/guild/roles", headers={"X-Guild-ID": str(SNOWFLAKE_GUILD)})
            self.assertEqual(api_res.status_code, 200)
            api_json = api_res.get_json()
            self.assertEqual(api_json.get("bot_status"), "present")
            by_id = {r["id"]: r for r in api_json["results"]}
            self.assertIn(newer_pos0_id, by_id)
            self.assertTrue(by_id[newer_pos0_id]["assignable"])
            self.assertIsNone(by_id[newer_pos0_id]["unassignable_reason"])
            self.assertIn(str(SNOWFLAKE_GUILD), by_id)
            self.assertFalse(by_id[str(SNOWFLAKE_GUILD)]["assignable"])
            self.assertEqual(by_id[str(SNOWFLAKE_GUILD)]["unassignable_reason"], "everyone")

    # ── 2. ISSUE B: Server Discovery Pagination & Indeterminate Handling ───

    def test_issue_b_server_discovery_pagination_and_failure_preservation(self):
        # 2a. Missing token must mark ok=False (not genuine empty)
        with patch.dict(os.environ, {"DISCORD_TOKEN": ""}, clear=False):
            missing = auth.fetch_bot_guilds_full()
            self.assertEqual(list(missing), [])
            self.assertFalse(getattr(missing, "ok", True))
            self.assertEqual(getattr(missing, "status", None), "missing_token")

        # 2b. Rate limit / network failure in /api/user/servers must NOT downgrade granted server to dangling
        self._login(guild_id=None)
        user_guilds = auth.GuildDiscoveryList(
            [
                {"id": str(SNOWFLAKE_GUILD), "name": "Granted Active", "permissions": "8", "icon": None},
                {"id": "1088248319982006999", "name": "Other Manageable", "permissions": "32", "icon": None},
            ],
            ok=True,
            status="ok",
        ) if hasattr(auth, "GuildDiscoveryList") else []

        with patch("dashboard.auth.requests.get", return_value=_mock_resp(429, {"retry_after": 3.0})), \
             patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertTrue(body.get("degraded"), "Failed bot guild fetch must mark response degraded")
            by_id = {s["id_str"]: s for s in body["servers"]}
            self.assertEqual(by_id[str(SNOWFLAKE_GUILD)]["category"], "active")
            self.assertNotEqual(by_id["1088248319982006999"]["category"], "invite")

        # 2c. Pagination across 200-guild boundary (continues until empty page [])
        page1 = [{"id": str(1000 + i), "name": f"G{i}"} for i in range(200)]
        page2 = [{"id": str(2000 + i), "name": f"G2_{i}"} for i in range(5)]
        seen_params = []

        def fake_pages(url, headers=None, params=None, timeout=None):
            seen_params.append(dict(params or {}))
            after = (params or {}).get("after")
            if after is None:
                return _mock_resp(200, page1)
            if after == "1199":
                return _mock_resp(200, page2)
            if after == "2004":
                return _mock_resp(200, [])
            raise AssertionError(f"Unexpected pagination params: {params}")

        with patch("dashboard.auth.requests.get", side_effect=fake_pages):
            full = auth.fetch_bot_guilds_full()
            self.assertTrue(getattr(full, "ok", False))
            self.assertTrue(getattr(full, "complete", False))
            self.assertEqual(len(full), 205)
            self.assertEqual(len(seen_params), 3)
            self.assertEqual(seen_params[0].get("limit"), 200)
            self.assertEqual(seen_params[1].get("after"), "1199")
            self.assertEqual(seen_params[2].get("after"), "2004")

    def test_issue_b_short_non_empty_page_199_then_3_then_empty(self):
        page1 = [{"id": str(1000 + i), "name": f"G1_{i}"} for i in range(199)]
        page2 = [{"id": str(1199 + i), "name": f"G2_{i}"} for i in range(3)]
        seen_params = []

        def fake_short_page(url, headers=None, params=None, timeout=None):
            seen_params.append(dict(params or {}))
            after = (params or {}).get("after")
            if after is None:
                return _mock_resp(200, page1)
            if after == "1198":
                return _mock_resp(200, page2)
            if after == "1201":
                return _mock_resp(200, [])
            raise AssertionError(f"Unexpected pagination params: {params}")

        with patch("dashboard.auth.requests.get", side_effect=fake_short_page):
            full = auth.fetch_bot_guilds_full()
            self.assertEqual(len(seen_params), 3)
            self.assertEqual(seen_params[1].get("after"), "1198")
            self.assertEqual(seen_params[2].get("after"), "1201")
            self.assertEqual(len(full), 202)
            self.assertEqual(
                [full[0]["id"], full[198]["id"], full[199]["id"], full[201]["id"]],
                ["1000", "1198", "1199", "1201"],
            )
            self.assertTrue(full.ok)
            self.assertTrue(full.complete)
            self.assertEqual(full.status, "ok")

    def test_issue_b_repeated_cursor_stops_immediately_as_incomplete(self):
        # Adversarial Mock: simulates a broken upstream API ignoring `after` and
        # repeating the exact same page/cursor.
        page1 = [{"id": str(1000 + i), "name": f"G_{i}"} for i in range(200)]
        seen_params = []

        def fake_repeated_cursor(url, headers=None, params=None, timeout=None):
            seen_params.append(dict(params or {}))
            return _mock_resp(200, page1)

        with patch("dashboard.auth.requests.get", side_effect=fake_repeated_cursor):
            res = auth.fetch_bot_guilds_full()
            self.assertEqual(len(seen_params), 2)
            self.assertEqual(seen_params[1].get("after"), "1199")
            self.assertEqual(len(res), 200)
            self.assertFalse(res.ok)
            self.assertFalse(res.complete)
            self.assertEqual(res.status, "truncated")
            self.assertIn("1199", res.error or "")

    def test_issue_b_cursor_cycle_detected_before_page_cap(self):
        # Adversarial Mock: simulates a broken upstream API response whose third
        # page loops back to an earlier cursor rather than a natural Discord
        # pagination sequence; verifies defensive multi-step cycle detection while
        # strictly respecting limit=200 items per page.
        page_a = [{"id": str(1000 + i), "name": f"GA_{i}"} for i in range(200)]
        page_b = [{"id": str(1200 + i), "name": f"GB_{i}"} for i in range(200)]
        seen_params = []

        def fake_cycling_cursor(url, headers=None, params=None, timeout=None):
            seen_params.append(dict(params or {}))
            after = (params or {}).get("after")
            if after is None:
                return _mock_resp(200, page_a)
            if after == "1199":
                return _mock_resp(200, page_b)
            if after == "1399":
                return _mock_resp(200, page_a)
            raise AssertionError(f"Unexpected pagination params: {params}")

        with patch("dashboard.auth.requests.get", side_effect=fake_cycling_cursor):
            res = auth.fetch_bot_guilds_full()
            self.assertEqual(len(seen_params), 3)
            self.assertEqual([p.get("after") for p in seen_params], [None, "1199", "1399"])
            self.assertEqual(len(res), len({g["id"] for g in res}))
            self.assertEqual(len(res), 400)
            self.assertFalse(res.ok)
            self.assertFalse(res.complete)
            self.assertEqual(res.status, "truncated")
            self.assertIn("1199", res.error or "")

    def test_issue_b_pagination_cap_marks_truncated_not_complete(self):
        call_idx = {"n": 0}

        def fake_infinite_pages(url, headers=None, params=None, timeout=None):
            base = call_idx["n"] * 200 + 10000
            call_idx["n"] += 1
            return _mock_resp(200, [{"id": str(base + i), "name": f"Guild_{base + i}"} for i in range(200)])

        with patch("dashboard.auth.requests.get", side_effect=fake_infinite_pages):
            capped = auth.fetch_bot_guilds_full()
            self.assertEqual(call_idx["n"], auth._MAX_GUILD_PAGES)
            self.assertEqual(len(capped), auth._MAX_GUILD_PAGES * 200)
            self.assertFalse(capped.complete, "Truncated 25-page list must set complete=False")
            self.assertFalse(capped.ok, "Truncated 25-page list must NOT report ok=True")
            self.assertEqual(capped.status, "truncated")

            bot_set = auth.fetch_discord_bot_guilds()
            self.assertFalse(bot_set.complete)
            self.assertFalse(bot_set.ok)
            self.assertEqual(bot_set.status, "truncated")

        # Verify /api/user/servers treats truncated bot list as degraded:
        # - Guilds inside the returned 5,000 ARE recognized as installed ("active" when granted)
        # - Guilds outside the returned 5,000 are "indeterminate", NEVER "dangling" or "invite"
        self._login(guild_id=None)
        in_window_guild = "10005"
        beyond_window_guild = "999999999999999999"
        user_guilds = auth.GuildDiscoveryList(
            [
                {"id": in_window_guild, "name": "Seen In First 5000", "permissions": "8", "icon": None},
                {"id": beyond_window_guild, "name": "Beyond Page 25", "permissions": "32", "icon": None},
            ],
            ok=True,
            complete=True,
            status="ok",
        )
        call_idx["n"] = 0
        with patch("dashboard.auth.requests.get", side_effect=fake_infinite_pages), \
             patch("dashboard.app.fetch_discord_guilds", return_value=user_guilds):
            resp = self.client.get("/api/user/servers")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertTrue(body["degraded"])
            self.assertFalse(body["bot_discovery_complete"])
            self.assertEqual(body["bot_discovery_status"], "truncated")
            by_id = {s["id_str"]: s for s in body["servers"]}
            self.assertEqual(by_id[in_window_guild]["bot_membership_status"], "installed")
            self.assertEqual(by_id[beyond_window_guild]["bot_membership_status"], "indeterminate")
            self.assertEqual(by_id[beyond_window_guild]["category"], "indeterminate")

    # ── 3. ISSUE C: Select / Option Dark & Light CSS + Stylesheet Order ────

    def test_issue_c_select_css_and_stylesheet_order(self):
        with open("dashboard/templates/base.html", encoding="utf-8") as f:
            base_html = f.read()
        idx_select2 = base_html.find("select2.min.css")
        idx_main = base_html.find("css/main.css")
        self.assertGreater(idx_select2, -1)
        self.assertGreater(idx_main, -1)
        self.assertLess(idx_select2, idx_main, "select2.min.css must load BEFORE main.css")

        with open("dashboard/static/css/main.css", encoding="utf-8") as f:
            css = f.read()
        self.assertIn("color-scheme: dark", css)
        self.assertIn("color-scheme: light", css)
        self.assertIn("select option", css)
        self.assertIn("select optgroup", css)
        self.assertIn(".select2-results__option--selected", css)

    # ── 4. ISSUE D & E: load_dotenv() Order & Cross-Connection DB Sync ─────

    def test_issue_d_load_dotenv_precedes_database_and_auth_imports(self):
        with open("dashboard/app.py", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="dashboard/app.py")

        dotenv_line = None
        db_import_line = None
        auth_import_line = None
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fn = node.value.func
                if isinstance(fn, ast.Name) and fn.id == "load_dotenv":
                    dotenv_line = dotenv_line or node.lineno
            elif isinstance(node, ast.ImportFrom):
                if node.module == "database":
                    db_import_line = db_import_line or node.lineno
                elif node.module and node.module.startswith("dashboard.auth"):
                    auth_import_line = auth_import_line or node.lineno

        self.assertIsNotNone(dotenv_line, "dashboard/app.py must call load_dotenv() at top level")
        self.assertLess(dotenv_line, db_import_line, "load_dotenv() must execute BEFORE importing database")
        self.assertLess(dotenv_line, auth_import_line, "load_dotenv() must execute BEFORE importing dashboard.auth")

    # ── 5. SECURITY: SSRF, Form CSRF, /config/access & Role Validation ─────

    def test_security_ssrf_blocked_in_bot_profile_image_fetch(self):
        unsafe_urls = [
            "file:///etc/passwd",
            "ftp://example.com/a.png",
            "http://127.0.0.1:8080/secret.png",
            "http://localhost/secret.png",
            "http://169.254.169.254/latest/meta-data/",
            "https://10.0.0.5/internal.png",
            "https://[::1]/loopback.png",
            "https://[64:ff9b::7f00:1]/nat64-loopback.png",
            "https://[::ffff:127.0.0.1]/v4mapped-loopback.png",
        ]
        with patch("utils.bot_profile.socket.socket") as mock_sock:
            for u in unsafe_urls:
                data_uri, err = bot_profile._download_image_as_data_uri(u)
                self.assertIsNone(data_uri, f"Unsafe URL {u} must be rejected")
                self.assertIsNotNone(err)
            mock_sock.assert_not_called()

    def test_security_ssrf_dns_rebinding_mixed_rrset_redirects_tls_and_cleanup(self):
        import http.server
        import ipaddress
        import socket
        import ssl
        import subprocess
        import threading

        # 1. Mixed public + private DNS RRset MUST be rejected before dialing any socket
        mixed_infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        ]
        with patch("utils.bot_profile.socket.getaddrinfo", return_value=mixed_infos), \
             patch("utils.bot_profile.socket.socket") as mock_sock:
            uri, err = bot_profile._download_image_as_data_uri("https://mixed.example/avatar.png")
            self.assertIsNone(uri)
            self.assertIn("private or non-public", err or "")
            mock_sock.assert_not_called()

        # 2. Redirect to localhost / private IP MUST close hop-1 response and block hop-2 without dialing
        closed_flags = {"redirect": False, "bad_ctype": False, "big_cl": False, "big_stream": False}

        def _make_tracked_resp(flag_key, status=200, headers=None, chunks=None):
            r = MagicMock()
            r.status_code = status
            r.headers = headers or {}
            r.iter_content = lambda chunk_size=65536: iter(chunks or [])
            r.close.side_effect = lambda: closed_flags.__setitem__(flag_key, True)
            return r

        public_only = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("utils.bot_profile.socket.getaddrinfo", return_value=public_only), \
             patch("requests.Session.get", return_value=_make_tracked_resp(
                 "redirect", status=302, headers={"Location": "http://127.0.0.1:8080/admin.png"}
             )) as mock_sess_get:
            uri, err = bot_profile._download_image_as_data_uri("https://cdn.example/start.png")
            self.assertIsNone(uri)
            self.assertTrue(closed_flags["redirect"], "Redirect response must be closed")
            self.assertEqual(mock_sess_get.call_count, 1, "Private redirect target must be blocked before second GET")

        # 3. Response cleanup on non-image Content-Type, oversized Content-Length, and mid-stream overflow
        with patch("utils.bot_profile.socket.getaddrinfo", return_value=public_only):
            with patch("requests.Session.get", return_value=_make_tracked_resp(
                "bad_ctype", status=200, headers={"Content-Type": "text/html"}
            )):
                uri, err = bot_profile._download_image_as_data_uri("https://cdn.example/page.html")
                self.assertIsNone(uri)
                self.assertTrue(closed_flags["bad_ctype"])

            with patch("requests.Session.get", return_value=_make_tracked_resp(
                "big_cl", status=200, headers={"Content-Type": "image/png", "Content-Length": "99999999"}
            )):
                uri, err = bot_profile._download_image_as_data_uri("https://cdn.example/huge.png", max_bytes=1024)
                self.assertIsNone(uri)
                self.assertTrue(closed_flags["big_cl"])

            with patch("requests.Session.get", return_value=_make_tracked_resp(
                "big_stream", status=200, headers={"Content-Type": "image/png"}, chunks=[b"x" * 700, b"y" * 700]
            )):
                uri, err = bot_profile._download_image_as_data_uri("https://cdn.example/stream.png", max_bytes=1024)
                self.assertIsNone(uri)
                self.assertTrue(closed_flags["big_stream"])

        # 4. End-to-end DNS rebinding / TOCTOU + TLS SNI + SAN hostname verification + Host header test
        with tempfile.TemporaryDirectory() as cert_dir:
            key_path = os.path.join(cert_dir, "key.pem")
            cert_path = os.path.join(cert_dir, "cert.pem")
            subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key_path, "-out", cert_path, "-days", "1",
                    "-subj", "/CN=cdn-rebind.example",
                    "-addext", "subjectAltName=DNS:cdn-rebind.example",
                ],
                check=True,
                capture_output=True,
            )

            seen_requests = []

            class TLSHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    seen_requests.append({"host": self.headers.get("Host"), "path": self.path})
                    payload = b"\x89PNG\r\n\x1a\npinned-ok"
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, *args):
                    return None

            class QuietTLSServer(http.server.ThreadingHTTPServer):
                def handle_error(self, request, client_address):
                    return None

            srv = QuietTLSServer(("127.0.0.1", 0), TLSHandler)
            tls_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
            srv.socket = tls_ctx.wrap_socket(srv.socket, server_side=True)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            port = srv.server_address[1]

            real_is_unsafe = bot_profile._is_unsafe_ip

            def allow_loopback_fixture_only(ip_obj):
                if ip_obj == ipaddress.ip_address("127.0.0.1"):
                    return False
                return real_is_unsafe(ip_obj)

            dns_calls = {"count": 0}

            def rebinding_getaddrinfo(host, port_arg, *args, **kwargs):
                dns_calls["count"] += 1
                if dns_calls["count"] > 1:
                    # Simulate attacker DNS rebinding to an internal IP on any second resolution
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port_arg))]
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port_arg))]

            try:
                with patch("utils.bot_profile._is_unsafe_ip", side_effect=allow_loopback_fixture_only), \
                     patch("utils.bot_profile.socket.getaddrinfo", side_effect=rebinding_getaddrinfo):
                    uri, err = bot_profile._download_image_as_data_uri(
                        f"https://cdn-rebind.example:{port}/avatar.png",
                        verify_tls=cert_path,
                    )
                    self.assertIsNone(err)
                    self.assertTrue((uri or "").startswith("data:image/png;base64,"))
                    self.assertEqual(dns_calls["count"], 1, "Must resolve DNS once and dial pinned IP without re-resolving")
                    self.assertEqual(seen_requests[0]["host"], f"cdn-rebind.example:{port}")

                # Certificate hostname mismatch MUST still fail TLS verification even when pinned IP is identical
                dns_calls["count"] = 0
                with patch("utils.bot_profile._is_unsafe_ip", side_effect=allow_loopback_fixture_only), \
                     patch("utils.bot_profile.socket.getaddrinfo", side_effect=rebinding_getaddrinfo):
                    bad_uri, bad_err = bot_profile._download_image_as_data_uri(
                        f"https://wrong-hostname.example:{port}/avatar.png",
                        verify_tls=cert_path,
                    )
                    self.assertIsNone(bad_uri)
                    self.assertIsNotNone(bad_err)
                    self.assertEqual(len(seen_requests), 1, "Mismatched TLS hostname must abort before sending HTTP request")
            finally:
                srv.shutdown()
                srv.server_close()
                t.join(timeout=5)

    def test_security_html_config_forms_require_csrf_and_validate_access_input(self):
        self._login()
        for route in ("/config/general", "/config/welcome", "/config/boost", "/config/access"):
            resp = self.client.post(route, data={"guild_id": str(SNOWFLAKE_GUILD)})
            self.assertEqual(resp.status_code, 403, f"{route} must reject missing csrf_token with 403")

        # Non-numeric user_id and invalid level on /config/access must NOT raise 500 ValueError
        bad_uid = self.client.post(
            "/config/access",
            data={
                "guild_id": str(SNOWFLAKE_GUILD),
                "csrf_token": "csrf_valid_token_123",
                "action": "add",
                "user_id": "not_a_snowflake",
                "level": "moderator",
            },
        )
        self.assertEqual(bad_uid.status_code, 400)

        bad_lvl = self.client.post(
            "/config/access",
            data={
                "guild_id": str(SNOWFLAKE_GUILD),
                "csrf_token": "csrf_valid_token_123",
                "action": "add",
                "user_id": "222333444555666777",
                "level": "super_root_hacker",
            },
        )
        self.assertEqual(bad_lvl.status_code, 400)

    def test_role_validation_enforced_on_welcome_boost_and_tagpartners_apis(self):
        self._login()
        roles = [
            {"id": str(SNOWFLAKE_GUILD), "name": "@everyone", "position": 0, "color": 0, "managed": False},
            {"id": str(SNOWFLAKE_ROLE_A), "name": "AboveBotAdmin", "position": 10, "color": 0, "managed": False},
            {"id": "1088248319982006350", "name": "Nero", "position": 3, "color": 0, "managed": True},
        ]
        bot_m = {"user": {"id": BOT_USER_ID}, "roles": ["1088248319982006350"], "permissions": str(rv.MANAGE_ROLES_PERM)}
        headers = {"X-CSRF-Token": "csrf_valid_token_123", "X-Guild-ID": str(SNOWFLAKE_GUILD)}

        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member", return_value=(roles, bot_m, None)):
            r1 = self.client.post("/api/settings/welcome", headers=headers, json={"auto_role_id": str(SNOWFLAKE_ROLE_A)})
            self.assertEqual(r1.status_code, 400)

            r2 = self.client.post("/api/settings/boost", headers=headers, json={"boost_role_id": str(SNOWFLAKE_ROLE_A)})
            self.assertEqual(r2.status_code, 400)

            r3 = self.client.post(
                "/api/tagpartners/save",
                headers=headers,
                json={
                    "partner_guild_id": "1088248319982006888",
                    "partner_label": "Partner",
                    "reward_type": "role",
                    "reward_role_id": str(SNOWFLAKE_ROLE_A),
                },
            )
            self.assertEqual(r3.status_code, 400)

        for tpl_path, elem_id in (
            ("dashboard/templates/systems/tagmissions.html", 'id="tm-role"'),
            ("dashboard/templates/systems/tagpartners.html", 'id="tp-role"'),
            ("dashboard/templates/manage/embedbuilder.html", 'id="eb-btn-role"'),
        ):
            with open(tpl_path, encoding="utf-8") as f:
                html = f.read()
            pos = html.find(elem_id)
            self.assertGreater(pos, -1, f"{elem_id} missing in {tpl_path}")
            tag_slice = html[max(0, pos - 120) : pos + 160]
            self.assertIn('data-assignable-only="true"', tag_slice, f"{elem_id} must opt in to data-assignable-only")

    # ── 6. 64-BIT SNOWFLAKES & E2E SHOP / LEVELING / SYNC WORKFLOWS ────────

    def test_snowflake_precision_preserved_across_dashboard_json_apis(self):
        # Verify test snowflakes genuinely lose precision if serialized as IEEE-754 float64
        self.assertNotEqual(int(float(SNOWFLAKE_ROLE_A)), SNOWFLAKE_ROLE_A)
        self.assertNotEqual(int(float(SNOWFLAKE_ROLE_B)), SNOWFLAKE_ROLE_B)
        self.assertNotEqual(int(float(SNOWFLAKE_CHAN_A)), SNOWFLAKE_CHAN_A)

        self._login()
        headers = {"X-CSRF-Token": "csrf_valid_token_123", "X-Guild-ID": str(SNOWFLAKE_GUILD)}

        roles = [
            {"id": str(SNOWFLAKE_GUILD), "name": "@everyone", "position": 0, "color": 0, "managed": False},
            {"id": str(SNOWFLAKE_ROLE_A), "name": "ShopVIP", "position": 1, "color": 0, "managed": False},
            {"id": str(SNOWFLAKE_ROLE_B), "name": "ReqRole", "position": 2, "color": 0, "managed": False},
            {"id": "1088248319982006399", "name": "Nero", "position": 9, "color": 0, "managed": True},
        ]
        bot_m = {"user": {"id": BOT_USER_ID}, "roles": ["1088248319982006399"], "permissions": str(rv.MANAGE_ROLES_PERM)}

        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member", return_value=(roles, bot_m, None)):
            add_res = self.client.post(
                "/api/shop/item",
                headers=headers,
                json={
                    "name": "Snowflake VIP Role",
                    "type": "role",
                    "price": 250,
                    "role_id": str(SNOWFLAKE_ROLE_A),
                    "required_role_id": str(SNOWFLAKE_ROLE_B),
                    "description": "Exact 64-bit role ID test",
                },
            )
            self.assertEqual(add_res.status_code, 200, add_res.get_data(as_text=True))

        # Fetch item via GET /api/shop/item/<id> and verify raw JSON preserves exact string digits
        async def _get_shop_id():
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT id FROM shop_items WHERE guild_id=? AND name='Snowflake VIP Role'",
                    (SNOWFLAKE_GUILD,),
                )
                return (await cur.fetchone())[0]

        item_id = run_async(_get_shop_id())
        item_res = self.client.get(f"/api/shop/item/{item_id}", headers={"X-Guild-ID": str(SNOWFLAKE_GUILD)})
        self.assertEqual(item_res.status_code, 200)
        item_json = item_res.get_json()["item"]
        self.assertEqual(item_json["role_id"], str(SNOWFLAKE_ROLE_A))
        self.assertEqual(item_json["required_role_id"], str(SNOWFLAKE_ROLE_B))

        # Seed and verify Leveling, MVP, Tickets, TagMissions, TagPartners, Moderation, Commands, Minigames snowflakes
        async def _seed_snowflakes():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO leveling_config (guild_id, levelup_channel_id) VALUES (?, ?)",
                    (SNOWFLAKE_GUILD, SNOWFLAKE_CHAN_A),
                )
                await db.execute(
                    "INSERT INTO leveling_bonus_roles (guild_id, role_id, multiplier) VALUES (?, ?, 1.5)",
                    (SNOWFLAKE_GUILD, SNOWFLAKE_ROLE_A),
                )
                await db.execute(
                    "INSERT INTO leveling_blacklist_roles (guild_id, role_id) VALUES (?, ?)",
                    (SNOWFLAKE_GUILD, SNOWFLAKE_ROLE_B),
                )
                await db.execute(
                    "INSERT OR REPLACE INTO mvp_config (guild_id, mvp_role_id, announce_channel_id) VALUES (?, ?, ?)",
                    (SNOWFLAKE_GUILD, SNOWFLAKE_ROLE_A, SNOWFLAKE_CHAN_A),
                )
                await db.execute(
                    "INSERT OR REPLACE INTO ticket_settings (guild_id, transcript_channel_id, support_role_id) VALUES (?, ?, ?)",
                    (SNOWFLAKE_GUILD, SNOWFLAKE_CHAN_A, SNOWFLAKE_ROLE_A),
                )
                await db.execute(
                    "INSERT INTO tag_missions (guild_id, title, channel_id, starts_at, ends_at, reward_type, reward_role_id, status) "
                    "VALUES (?, 'Snowflake Mission', ?, '2026-11-12 00:00:00', '2026-11-19 00:00:00', 'role', ?, 'scheduled')",
                    (SNOWFLAKE_GUILD, SNOWFLAKE_CHAN_A, SNOWFLAKE_ROLE_A),
                )
                await db.execute(
                    "INSERT INTO tag_partner_rewards (guild_id, partner_guild_id, partner_label, reward_type, reward_role_id) "
                    "VALUES (?, ?, 'PartnerSnowflake', 'role', ?)",
                    (SNOWFLAKE_GUILD, SNOWFLAKE_USER_A, SNOWFLAKE_ROLE_A),
                )
                await db.execute(
                    "INSERT INTO warning_thresholds (guild_id, warn_count, action, role_id) VALUES (?, 3, 'add_role', ?)",
                    (SNOWFLAKE_GUILD, SNOWFLAKE_ROLE_A),
                )
                await db.execute(
                    "INSERT OR REPLACE INTO command_toggles (guild_id, command_name, enabled, enabled_roles, disabled_channels) "
                    "VALUES (?, 'ping', 1, ?, ?)",
                    (SNOWFLAKE_GUILD, json.dumps([SNOWFLAKE_ROLE_A]), json.dumps([SNOWFLAKE_CHAN_A])),
                )
                await db.commit()

        run_async(_seed_snowflakes())

        lvl_cfg = self.client.get("/api/leveling/config", headers=headers).get_json()["config"]
        self.assertEqual(lvl_cfg["levelup_channel_id"], str(SNOWFLAKE_CHAN_A))

        bonus = self.client.get("/api/leveling/bonus-roles", headers=headers).get_json()["roles"]
        self.assertEqual(bonus[0]["role_id"], str(SNOWFLAKE_ROLE_A))

        bl = self.client.get("/api/leveling/blacklist", headers=headers).get_json()["roles"]
        self.assertEqual(bl[0]["role_id"], str(SNOWFLAKE_ROLE_B))

        mvp = self.client.get("/api/mvp/config", headers=headers).get_json()["config"]
        self.assertEqual(mvp["mvp_role_id"], str(SNOWFLAKE_ROLE_A))
        self.assertEqual(mvp["announce_channel_id"], str(SNOWFLAKE_CHAN_A))

        tkt = self.client.get("/api/tickets/settings", headers=headers).get_json()
        self.assertEqual(tkt["transcript_channel_id"], str(SNOWFLAKE_CHAN_A))
        self.assertEqual(tkt["support_role_id"], str(SNOWFLAKE_ROLE_A))

        tm = self.client.get("/api/tagmissions/list", headers=headers).get_json()
        self.assertEqual(tm[0]["channel_id"], str(SNOWFLAKE_CHAN_A))
        self.assertEqual(tm[0]["reward_role_id"], str(SNOWFLAKE_ROLE_A))

        tp = self.client.get("/api/tagpartners/list", headers=headers).get_json()
        self.assertEqual(tp[0]["partner_guild_id"], str(SNOWFLAKE_USER_A))
        self.assertEqual(tp[0]["reward_role_id"], str(SNOWFLAKE_ROLE_A))

        wt = self.client.get("/api/moderation/warning-thresholds", headers=headers).get_json()
        self.assertEqual(wt[0]["role_id"], str(SNOWFLAKE_ROLE_A))

        cmd_cfg = self.client.get("/api/commands/settings/ping", headers=headers).get_json()
        self.assertEqual(json.loads(cmd_cfg["enabled_roles"]), [str(SNOWFLAKE_ROLE_A)])
        self.assertEqual(json.loads(cmd_cfg["disabled_channels"]), [str(SNOWFLAKE_CHAN_A)])

        mg_save = self.client.post(
            "/api/minigames/config",
            headers=headers,
            json={"enabled": True, "channel_id": str(SNOWFLAKE_CHAN_A)},
        )
        self.assertEqual(mg_save.status_code, 200)
        mg_cfg = self.client.get("/api/minigames/config", headers=headers).get_json()["config"]
        self.assertEqual(mg_cfg["channel_id"], SNOWFLAKE_CHAN_A)
        self.assertEqual(mg_cfg["channel_id_str"], str(SNOWFLAKE_CHAN_A))

    # ── 7. ISSUE E: DASHBOARD <-> BOT WAL SYNC, ICON UPLOAD & SERVER SWITCH ─

    def test_issue_e_dashboard_bot_wal_sync_upload_and_server_switch(self):
        import base64
        import io
        from utils.xp_calculator import get_leveling_config

        guild_b = 1088248319982006999
        self._login()
        headers_a = {"X-CSRF-Token": "csrf_valid_token_123", "X-Guild-ID": str(SNOWFLAKE_GUILD)}
        headers_b = {"X-CSRF-Token": "csrf_valid_token_123", "X-Guild-ID": str(guild_b)}

        # 1. Dashboard -> Bot sync: save Leveling & Economy settings on Guild A and read via Bot helper / fresh connection
        lvl_post = self.client.post(
            "/api/leveling/config",
            headers=headers_a,
            json={
                "message_xp_enabled": 1,
                "xp_min_per_message": 22,
                "xp_max_per_message": 44,
                "xp_cooldown_seconds": 45,
                "levelup_channel_id": str(SNOWFLAKE_CHAN_A),
            },
        )
        self.assertEqual(lvl_post.status_code, 200)
        bot_seen_cfg = run_async(get_leveling_config(SNOWFLAKE_GUILD))
        self.assertEqual(bot_seen_cfg["xp_min_per_message"], 22)
        self.assertEqual(bot_seen_cfg["xp_max_per_message"], 44)
        self.assertEqual(int(bot_seen_cfg["levelup_channel_id"]), SNOWFLAKE_CHAN_A)

        cur_post = self.client.post(
            "/api/economy/currency",
            headers=headers_a,
            json={"currency_name": "StarBits", "coin_emoji_id": "⭐", "diamond_name": "Shards", "diamond_emoji_id": "💎"},
        )
        self.assertEqual(cur_post.status_code, 200)

        from utils.currency import get_currency_config

        async def _bot_check_currency_and_write_user():
            bot_cur = await get_currency_config(SNOWFLAKE_GUILD)
            async with aiosqlite.connect(DB_PATH) as bot_db:
                # Simulate Bot cog writing member economy balance in WAL mode
                await bot_db.execute(
                    "INSERT OR REPLACE INTO economy (user_id, guild_id, balance) VALUES (?, ?, 7777)",
                    (SNOWFLAKE_USER_A, SNOWFLAKE_GUILD),
                )
                await bot_db.commit()
            return (bot_cur["coins"]["name"], bot_cur["diamonds"]["name"])

        bot_currency_row = run_async(_bot_check_currency_and_write_user())
        self.assertEqual(bot_currency_row, ("StarBits", "Shards"))

        # 2. Bot -> Dashboard sync: Dashboard immediately sees Bot-written leaderboard entry
        lb_html = self.client.get("/api/economy/leaderboard", headers=headers_a).get_data(as_text=True)
        self.assertTrue("7,777" in lb_html or "7777" in lb_html, f"Expected 7777 in {lb_html}")

        # 3. Multipart upload workflow: Embed Builder attachment send via multipart/form-data
        png_1x1 = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        mock_discord_ch_get = MagicMock(status_code=200)
        mock_discord_ch_get.json.return_value = {"id": str(SNOWFLAKE_CHAN_A), "guild_id": str(SNOWFLAKE_GUILD), "type": 0}
        mock_discord_post = MagicMock(status_code=200)
        mock_discord_post.json.return_value = {"id": "1088248319982006777", "channel_id": str(SNOWFLAKE_CHAN_A)}
        with patch.dict(os.environ, {"DISCORD_TOKEN": "test_bot_token"}, clear=False):
            with patch("requests.get", return_value=mock_discord_ch_get), \
                 patch("requests.post", return_value=mock_discord_post) as posted:
                up_res = self.client.post(
                    "/api/embedbuilder/send",
                    headers=headers_a,
                    data={
                        "channel_id": str(SNOWFLAKE_CHAN_A),
                        "payload_json": json.dumps({"content": "Hello with PNG", "embeds": []}),
                        "files": (io.BytesIO(png_1x1), "banner.png"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(up_res.status_code, 200, up_res.get_data(as_text=True))
                self.assertTrue(up_res.get_json().get("success"))
                self.assertTrue(posted.called)

        # 4. Multi-guild server-context switch isolation: Guild B does NOT see Guild A's custom currency or shop items
        cur_b = self.client.get("/api/economy/currency", headers=headers_b).get_json()
        coins_b = (cur_b.get("coins") or {}).get("name") or cur_b.get("coins_name")
        self.assertNotEqual(coins_b, "StarBits")
        items_b = self.client.get("/api/shop/items", headers=headers_b).get_json()
        items_b_list = items_b if isinstance(items_b, list) else items_b.get("items", [])
        self.assertEqual(len(items_b_list), 0)

    def test_issue_e_two_process_isolated_sqlite_wal_sync(self):
        """
        Spawns an independent OS child Python process against the isolated temporary
        SQLite WAL database (`DB_PATH`) to verify cross-process WAL visibility between
        the Flask Dashboard process and a separate Bot-side SQLite/utility process.
        (Note: verifies local two-OS-process SQLite WAL synchronization; live Railway
        volume + Discord gateway deployment remains a staging smoke check.)
        """
        import subprocess

        self._login()
        headers_a = {"X-CSRF-Token": "csrf_valid_token_123", "X-Guild-ID": str(SNOWFLAKE_GUILD)}

        # Dashboard process writes currency + leveling config to WAL DB
        r_cur = self.client.post(
            "/api/economy/currency",
            headers=headers_a,
            json={"currency_name": "TwoProcCoins", "coin_emoji_id": "🪙", "diamond_name": "TwoProcGems", "diamond_emoji_id": "💠"},
        )
        self.assertEqual(r_cur.status_code, 200)

        child_code = """
import asyncio, json, os, sys
import aiosqlite
from database import DB_PATH
from utils.currency import get_currency_config

async def main():
    guild_id = int(sys.argv[1])
    user_id = int(sys.argv[2])
    cur = await get_currency_config(guild_id)
    assert cur["coins"]["name"] == "TwoProcCoins", f"Unexpected coins: {cur}"
    assert cur["diamonds"]["name"] == "TwoProcGems", f"Unexpected diamonds: {cur}"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(
            "INSERT OR REPLACE INTO economy (user_id, guild_id, balance) VALUES (?, ?, 42424)",
            (user_id, guild_id),
        )
        await db.commit()
    print(json.dumps({"ok": True, "db": DB_PATH}))

asyncio.run(main())
"""
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-c", child_code, str(SNOWFLAKE_GUILD), str(SNOWFLAKE_USER_A)],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, f"Child process failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")

        # Still-running Dashboard process immediately reads child process's committed WAL balance
        lb_html = self.client.get("/api/economy/leaderboard", headers=headers_a).get_data(as_text=True)
        self.assertTrue("42,424" in lb_html or "42424" in lb_html, f"Expected 42,424 in {lb_html}")

    def test_all_dashboard_page_routes_render_without_500(self):
        """Render every major Dashboard HTML page route via Flask/Jinja2 and verify zero 500s."""
        self._login()
        page_routes = [
            "/login",
            "/server-select",
            "/",
            "/members",
            f"/members/{SNOWFLAKE_USER_A}",
            "/audit-log",
            "/reports",
            "/health",
            "/moderation",
            "/tickets",
            "/embed-builder",
            "/embed-builder/v2",
            "/reaction-roles",
            "/triggers",
            "/custom-commands",
            "/mvp",
            "/leveling",
            "/economy",
            "/shop",
            "/shop-publisher",
            "/shop-designer",
            "/events",
            "/minigames",
            "/minigames/builder",
            "/missions",
            "/ledger",
            "/inventory",
            f"/inventory/{SNOWFLAKE_USER_A}",
            "/trade",
            "/tag-missions",
            "/tag-partners",
            "/creator",
            "/config/general",
            "/config/welcome",
            "/config/boost",
            "/config/botprofile",
            "/config/announcements",
            "/commands",
            "/config/access",
        ]
        with patch("dashboard.auth.requests.get", return_value=_mock_resp(200, [])), \
             patch("dashboard.utils.role_validation._req.get", return_value=_mock_resp(200, [])):
            for route in page_routes:
                with self.subTest(route=route):
                    resp = self.client.get(route)
                    self.assertIn(resp.status_code, (200, 302), f"{route} returned unexpected HTTP {resp.status_code}")

            # /backups is restricted to developer (OWNER_ID=999999999): 403 for non-developer owner, 200 for developer
            self.assertEqual(self.client.get("/backups").status_code, 403)
            self._login(user_id="999999999")
            self.assertEqual(self.client.get("/backups").status_code, 200)

    # ── 8. PHASE 3 AUDIT REMEDIATIONS (F-01, F-02, F-03, F-07, D-1) ─────────

    def test_f01_embedbuilder_send_blocks_cross_guild_and_non_snowflake_channels(self):
        self._login(guild_id=SNOWFLAKE_GUILD, level="owner")
        headers = {"X-CSRF-Token": "csrf_valid_token_123", "X-Guild-ID": str(SNOWFLAKE_GUILD)}
        payload_json = json.dumps({"content": "Hello world", "embeds": [], "buttons": [], "select_menus": []})

        other_guild_id = 1360461358486913145
        same_guild_channel = "1088248319982006444"
        other_guild_channel = "1360461358486999999"
        dm_channel = "1360461358486888888"

        post_calls = []
        get_calls = []

        def fake_get(url, headers=None, timeout=None, **kwargs):
            get_calls.append(url)
            if url.endswith(f"/channels/{same_guild_channel}"):
                return _mock_resp(200, {"id": same_guild_channel, "guild_id": str(SNOWFLAKE_GUILD), "type": 0})
            if url.endswith(f"/channels/{other_guild_channel}"):
                return _mock_resp(200, {"id": other_guild_channel, "guild_id": str(other_guild_id), "type": 0})
            if url.endswith(f"/channels/{dm_channel}"):
                return _mock_resp(200, {"id": dm_channel, "type": 1})
            return _mock_resp(500, {"message": "Discord outage"})

        def fake_post(url, *args, **kwargs):
            post_calls.append(url)
            return _mock_resp(200, {"id": "1499999999999999999"})

        with patch("dashboard.api.embedbuilder._req.get", side_effect=fake_get), \
             patch("dashboard.api.embedbuilder._req.post", side_effect=fake_post), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-audit"}):

            # 1. Path traversal / non-digit channel_id -> 400 before any Discord HTTP request
            r_path = self.client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "123/../../users/@me", "payload_json": payload_json},
                headers=headers,
            )
            self.assertEqual(r_path.status_code, 400)
            self.assertEqual(len(get_calls), 0)
            self.assertEqual(len(post_calls), 0)

            # 2. Cross-guild channel_id -> 403 and NEVER posts to Discord
            r_cross = self.client.post(
                "/api/embedbuilder/send",
                data={"channel_id": other_guild_channel, "payload_json": payload_json},
                headers=headers,
            )
            self.assertEqual(r_cross.status_code, 403)
            self.assertEqual(len(post_calls), 0)

            # 3. Non-guild (DM) channel -> 403 and NEVER posts to Discord
            r_dm = self.client.post(
                "/api/embedbuilder/send",
                data={"channel_id": dm_channel, "payload_json": payload_json},
                headers=headers,
            )
            self.assertEqual(r_dm.status_code, 403)
            self.assertEqual(len(post_calls), 0)

            # 4. Upstream Discord verification failure -> fails closed (502) and NEVER posts
            r_err = self.client.post(
                "/api/embedbuilder/send",
                data={"channel_id": "1088248319982006555", "payload_json": payload_json},
                headers=headers,
            )
            self.assertEqual(r_err.status_code, 502)
            self.assertEqual(len(post_calls), 0)

            # 5. Valid channel owned by active session guild -> 200 and posts message
            r_ok = self.client.post(
                "/api/embedbuilder/send",
                data={"channel_id": same_guild_channel, "payload_json": payload_json},
                headers=headers,
            )
            self.assertEqual(r_ok.status_code, 200)
            self.assertEqual(len(post_calls), 1)

    def test_f02_administrator_permission_role_unassignable_even_below_bot(self):
        bot_role_id = "1088248319982006399"
        junior_admin_id = "1088248319982006350"
        safe_member_id = "1088248319982006351"
        roles = [
            {"id": str(SNOWFLAKE_GUILD), "name": "@everyone", "position": 0, "managed": False, "permissions": "0"},
            {"id": safe_member_id, "name": "Member", "position": 2, "managed": False, "permissions": "0"},
            {"id": junior_admin_id, "name": "Co-Owner", "position": 3, "managed": False, "permissions": str(rv.ADMINISTRATOR_PERM)},
            {"id": bot_role_id, "name": "Nero", "position": 10, "managed": True, "permissions": str(rv.MANAGE_ROLES_PERM)},
        ]
        bot_member = {"user": {"id": BOT_USER_ID}, "roles": [bot_role_id], "permissions": str(rv.MANAGE_ROLES_PERM)}

        info = rv.compute_role_assignability(SNOWFLAKE_GUILD, roles, bot_member)
        self.assertTrue(info["role_map"][safe_member_id]["assignable"])
        self.assertFalse(info["role_map"][junior_admin_id]["assignable"])
        self.assertEqual(info["role_map"][junior_admin_id]["unassignable_reason"], "dangerous_permissions")

        with patch("dashboard.utils.role_validation.fetch_guild_roles_and_bot_member", return_value=(roles, bot_member, None)):
            valid_new, err_new = rv.validate_assignable_role(SNOWFLAKE_GUILD, junior_admin_id)
            self.assertFalse(valid_new)
            self.assertIn("Administrator", err_new or "")

            # Delta Rule still preserves an unchanged existing binding so unrelated settings remain editable
            valid_delta, err_delta = rv.validate_assignable_role(
                SNOWFLAKE_GUILD, junior_admin_id, existing_role_id=junior_admin_id
            )
            self.assertTrue(valid_delta)
            self.assertIsNone(err_delta)

    def test_f07_external_guild_emojis_scoped_to_authorized_guilds_and_owner_only(self):
        authorized_other_guild = 1360461358486913145
        private_unrelated_guild = 1280983591148584971
        mod_uid = 222222222222222222
        adm_uid = 333333333333333333

        async def _seed_extra_grant():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) "
                    "VALUES (?, ?, 'admin', 1)",
                    (authorized_other_guild, self.admin_id),
                )
                await db.execute(
                    "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) "
                    "VALUES (?, ?, 'moderator', 1)",
                    (SNOWFLAKE_GUILD, mod_uid),
                )
                await db.execute(
                    "INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled) "
                    "VALUES (?, ?, 'admin', 1)",
                    (SNOWFLAKE_GUILD, adm_uid),
                )
                await db.commit()

        run_async(_seed_extra_grant())

        bot_guilds = auth.GuildDiscoveryList(
            [
                {"id": str(SNOWFLAKE_GUILD), "name": "Current Guild"},
                {"id": str(authorized_other_guild), "name": "Authorized Sister Guild"},
                {"id": str(private_unrelated_guild), "name": "Secret Unrelated Guild"},
            ],
            ok=True,
            complete=True,
            status="ok",
        )

        def fake_emoji_get(url, headers=None, timeout=None, **kwargs):
            if f"/guilds/{authorized_other_guild}/emojis" in url:
                return _mock_resp(200, [{"id": "777000000000000001", "name": "sister_ok", "animated": False}])
            if f"/guilds/{private_unrelated_guild}/emojis" in url:
                return _mock_resp(200, [{"id": "777000000000000002", "name": "secret_leak", "animated": False}])
            return _mock_resp(200, [])

        with patch("dashboard.auth.fetch_bot_guilds_full", return_value=bot_guilds), \
             patch("dashboard.api.core._req.get", side_effect=fake_emoji_get), \
             patch.dict(os.environ, {"DISCORD_TOKEN": "mock-token-audit"}):

            # Moderator & Admin cannot access owner-level Embed Builder external emoji endpoint
            self._login(guild_id=SNOWFLAKE_GUILD, level="moderator", user_id=str(mod_uid))
            self.assertEqual(self.client.get("/api/guild/emojis/external").status_code, 403)
            self._login(guild_id=SNOWFLAKE_GUILD, level="admin", user_id=str(adm_uid))
            self.assertEqual(self.client.get("/api/guild/emojis/external").status_code, 403)

            # Non-developer Owner of SNOWFLAKE_GUILD sees ONLY authorized_other_guild, NEVER private_unrelated_guild
            self._login(guild_id=SNOWFLAKE_GUILD, level="owner")
            resp = self.client.get("/api/guild/emojis/external")
            self.assertEqual(resp.status_code, 200)
            servers = resp.get_json()["servers"]
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0]["guild_id"], str(authorized_other_guild))
            self.assertEqual(servers[0]["guild_name"], "Authorized Sister Guild")

            # Trusted super-admin (OWNER_ID=999999999) sees all external bot guilds
            self._login(guild_id=SNOWFLAKE_GUILD, level="owner", user_id="999999999")
            resp_dev = self.client.get("/api/guild/emojis/external")
            self.assertEqual(resp_dev.status_code, 200)
            dev_ids = {s["guild_id"] for s in resp_dev.get_json()["servers"]}
            self.assertEqual(dev_ids, {str(authorized_other_guild), str(private_unrelated_guild)})

    def test_f03_and_d1_warning_threshold_validation_and_minigames_mutation_snowflakes(self):
        self._login(guild_id=SNOWFLAKE_GUILD, level="owner")
        headers = {"X-CSRF-Token": "csrf_valid_token_123", "X-Guild-ID": str(SNOWFLAKE_GUILD)}

        async def _clean_tables():
            from utils import minigame_store as store
            await store.ensure_tables()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM warning_thresholds WHERE guild_id = ?", (SNOWFLAKE_GUILD,))
                await db.execute("DELETE FROM minigame_templates WHERE guild_id = ?", (SNOWFLAKE_GUILD,))
                await db.execute("DELETE FROM minigame_categories WHERE guild_id = ?", (SNOWFLAKE_GUILD,))
                await db.execute("DELETE FROM minigames_log WHERE guild_id = ?", (SNOWFLAKE_GUILD,))
                await db.commit()

        run_async(_clean_tables())
        self.addCleanup(lambda: run_async(_clean_tables()))

        # 1. F-03: Invalid / XSS warning-threshold payloads are rejected with 400 (never stored or 500)
        for bad_payload in (
            {},
            {"warn_count": 3, "action": "<img src=x onerror=alert(1)>"},
            {"warn_count": 0, "action": "kick"},
            {"warn_count": True, "action": "kick"},
            {"warn_count": 3, "action": "timeout"},  # missing duration_minutes for timeout
            {"warn_count": 3, "action": "timeout", "duration_minutes": -5},
        ):
            r_bad = self.client.post("/api/moderation/warning-thresholds", json=bad_payload, headers=headers)
            self.assertEqual(r_bad.status_code, 400, f"Expected 400 for {bad_payload}, got {r_bad.status_code}")

        r_good = self.client.post(
            "/api/moderation/warning-thresholds",
            json={"warn_count": 3, "action": "timeout", "duration_minutes": 15},
            headers=headers,
        )
        self.assertEqual(r_good.status_code, 200)
        self.assertTrue(r_good.get_json()["success"])

        # 2. D-1: Minigames POST config, PATCH template, POST duplicate, and GET history carry exact *_str Snowflakes
        exact_chan = "1405993421167919336"
        exact_winner = 1360461358486913145

        cfg_res = self.client.post(
            "/api/minigames/config",
            json={"enabled": True, "channel_id": exact_chan, "min_events_per_week": 3, "max_events_per_week": 6},
            headers=headers,
        )
        self.assertEqual(cfg_res.status_code, 200)
        self.assertEqual(cfg_res.get_json()["config"]["channel_id_str"], exact_chan)

        cat_res = self.client.post("/api/minigames/categories", json={"name": "AuditCat"}, headers=headers)
        cat_id = cat_res.get_json()["category"]["id"]

        create_res = self.client.post(
            "/api/minigames/templates",
            json={
                "category_id": cat_id,
                "name": "AuditGame",
                "game_type": "quick_click",
                "channel_id": exact_chan,
                "config": {"buttons": 3, "reveal_min": 2, "reveal_max": 5, "wait_after": 10},
            },
            headers=headers,
        )
        self.assertTrue(create_res.get_json().get("success"), f"Template create failed: {create_res.get_json()}")
        tpl_id = create_res.get_json()["template"]["id"]

        patch_res = self.client.patch(
            f"/api/minigames/templates/{tpl_id}",
            json={"name": "AuditGameRenamed"},
            headers=headers,
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.get_json()["template"]["channel_id_str"], exact_chan)

        dup_res = self.client.post(
            f"/api/minigames/templates/{tpl_id}/duplicate",
            json={"new_name": "AuditGameCopy"},
            headers=headers,
        )
        self.assertEqual(dup_res.status_code, 200)
        self.assertEqual(dup_res.get_json()["template"]["channel_id_str"], exact_chan)

        async def _seed_history():
            from utils import minigame_store as store
            await store.ensure_tables()
            run_id = await store.start_run(
                SNOWFLAKE_GUILD, tpl_id, "AuditGameRenamed",
                cat_id, "AuditCat", "quick_click", "manual", int(exact_chan)
            )
            await store.finish_run(
                run_id,
                "completed",
                winners=[{"id": exact_winner, "name": "WinnerOne", "reward_type": "coins", "reward_value": "100", "status": "won"}],
            )

        run_async(_seed_history())
        hist_res = self.client.get("/api/minigames/history")
        self.assertEqual(hist_res.status_code, 200)
        hist_row = hist_res.get_json()["history"][0]
        self.assertEqual(hist_row["channel_id_str"], exact_chan)
        self.assertEqual(hist_row["winner_id_str"], str(exact_winner))
        self.assertEqual(hist_row["winners"][0]["id_str"], str(exact_winner))


if __name__ == "__main__":
    unittest.main()
