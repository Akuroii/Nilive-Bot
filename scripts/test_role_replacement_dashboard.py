"""Role Replacement — dashboard regression harness (scratch database, Flask client).

Pins the dashboard half of the feature:

  * the page renders and is gated by ``role_replacement`` in PAGE_PERMISSIONS;
  * CRUD through the real ``/api`` blueprint (create / list / update / toggle /
    delete), including the audit-log rows each write records;
  * CSRF: every mutating route refuses a missing or wrong ``X-CSRF-Token``;
  * guild isolation: a rule written for guild A is invisible to guild B, a
    session can only touch its own guild's rules, and a stale ``X-Guild-ID`` is
    rejected with 409 (and a malformed one with 400) before any handler runs;
  * role validation: managed / @everyone / unknown-bot-state roles are refused
    and nothing is persisted when the validator says no;
  * the configuration write is configuration only: no member role changes and
    no pending removal rows, i.e. no retroactive cleanup.

Run from the repository root:
    python scripts/test_role_replacement_dashboard.py
"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from phase1_support import GUILD, USER, execute, reset_database, rows  # noqa: E402

import dashboard.app as dashboard  # noqa: E402
from utils import role_replacement as RR  # noqa: E402

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

OTHER_GUILD = GUILD + 900
KEEP, CONFLICT_B, MANAGED, EVERYONE, UNKNOWN = 5101, 5102, 5103, 5104, 5199
CSRF = "role-replacement-csrf"
FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def seed_access(guild_id=GUILD, user_id=USER, level="admin"):
    execute("INSERT OR REPLACE INTO dashboard_users "
            "(guild_id, user_id, permission_level, enabled) VALUES (?,?,?,1)",
            (guild_id, user_id, level))


def client_for(guild_id=GUILD, level="admin", user_id=USER):
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user={"id": user_id, "username": "Tester"},
                       guild_id=guild_id, guild_name="Guild",
                       user_level=level, csrf_token=CSRF,
                       expires_at=time.time() + 3600)
    return client


def post(client, url, payload, *, csrf=CSRF, guild_header=None):
    headers = {}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    if guild_header is not None:
        headers["X-Guild-ID"] = str(guild_header)
    return client.post(url, json=payload, headers=headers)


def rules_for(guild_id=GUILD):
    return asyncio.run(RR.list_rules(guild_id))


def rule_count(guild_id=GUILD):
    return rows("SELECT COUNT(*) FROM role_replacement_rules WHERE guild_id=?",
                (guild_id,))[0][0]


def assignable_stub(*_args, **_kwargs):
    return True, None


def unassignable_reason(reason, label):
    def _inner(*_args, **_kwargs):
        return False, label
    return _inner


def main():
    asyncio.run(reset_database())
    seed_access()
    seed_access(OTHER_GUILD)
    client = client_for()

    # ── Page + permission gating ─────────────────────────────────────────
    response = client.get("/role-replacement")
    check("page renders for an admin", response.status_code == 200,
          str(response.status_code))
    check("page carries the rules table", b"rr-rules-body" in response.data)
    check("page is registered in PAGE_PERMISSIONS",
          "role_replacement" in __import__("utils.permissions",
                                           fromlist=["PAGE_PERMISSIONS"]).PAGE_PERMISSIONS)
    check("page requires admin, not moderator",
          __import__("utils.permissions",
                     fromlist=["PAGE_PERMISSIONS"]).PAGE_PERMISSIONS[
              "role_replacement"] == "admin")

    moderator = client_for(level="moderator")
    seed_access(level="moderator")
    check("moderator is refused the page",
          moderator.get("/role-replacement").status_code == 403)
    anonymous = app.test_client()
    check("unauthenticated page access redirects",
          anonymous.get("/role-replacement").status_code == 302)
    check("unauthenticated API access is 401",
          anonymous.get("/api/role-replacement/rules").status_code == 401)
    seed_access(level="admin")

    # ── CRUD ────────────────────────────────────────────────────────────
    with patch("dashboard.utils.role_validation.validate_assignable_role",
               assignable_stub):
        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": str(KEEP),
                         "conflict_role_ids": [str(CONFLICT_B)]})
        check("create: 200 with the stored rule", response.status_code == 200,
              response.get_data(as_text=True)[:200])
        created = (response.get_json() or {}).get("rule") or {}
        check("create: ids are normalised to strings",
              created.get("keep_role_id") == str(KEEP)
              and created.get("conflict_role_ids") == [str(CONFLICT_B)],
              str(created))
        check("create: persisted exactly once", rule_count() == 1)

        response = client.get("/api/role-replacement/rules")
        check("list: returns the rule",
              (response.get_json() or {}).get("rules", [{}])[0].get("keep_role_id")
              == str(KEEP), response.get_data(as_text=True)[:200])

        rule_id = created["id"]
        response = client.put(
            f"/api/role-replacement/rule/{rule_id}",
            json={"keep_role_id": str(KEEP),
                  "conflict_role_ids": [str(CONFLICT_B), "5105"]},
            headers={"X-CSRF-Token": CSRF})
        check("update: 200 and the conflicting set is replaced",
              response.status_code == 200
              and (response.get_json() or {}).get("rule", {})
              .get("conflict_role_ids") == [str(CONFLICT_B), "5105"],
              response.get_data(as_text=True)[:200])

        response = post(client, f"/api/role-replacement/rule/{rule_id}/toggle",
                        {"enabled": False})
        check("toggle: disabled", response.status_code == 200
              and (response.get_json() or {}).get("enabled") is False,
              response.get_data(as_text=True)[:200])
        check("toggle: stored as disabled",
              rows("SELECT enabled FROM role_replacement_rules WHERE id=?",
                   (rule_id,)) == [(0,)])

        response = client.delete(
            f"/api/role-replacement/rule/{rule_id}",
            headers={"X-CSRF-Token": CSRF})
        check("delete: 200", response.status_code == 200)
        check("delete: rule row and conflicts removed",
              rule_count() == 0
              and rows("SELECT COUNT(*) FROM role_replacement_conflicts") == [(0,)])
        check("delete: deleting twice is a 404",
              client.delete(f"/api/role-replacement/rule/{rule_id}",
                            headers={"X-CSRF-Token": CSRF}).status_code == 404)

        # Validation errors are rejected with a message, not a 500.
        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": str(KEEP),
                         "conflict_role_ids": [str(KEEP)]})
        check("create: self-conflict is a 400 with a message",
              response.status_code == 400
              and "keep role" in (response.get_json() or {}).get("error", ""),
              response.get_data(as_text=True)[:200])
        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": "nonsense",
                         "conflict_role_ids": [str(CONFLICT_B)]})
        check("create: malformed role id is a 400", response.status_code == 400)
        check("create: nothing was persisted by the rejected writes",
              rule_count() == 0)

        # The audit trail records each successful write.
        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": str(KEEP),
                         "conflict_role_ids": [str(CONFLICT_B)]})
        rule_id = (response.get_json() or {}).get("rule", {}).get("id")
        client.put(f"/api/role-replacement/rule/{rule_id}",
                   json={"keep_role_id": str(KEEP),
                         "conflict_role_ids": [str(CONFLICT_B)]},
                   headers={"X-CSRF-Token": CSRF})
        actions = [r[0] for r in rows(
            "SELECT action FROM audit_log WHERE guild_id=? "
            "AND page='role_replacement' ORDER BY id", (GUILD,))]
        check("audit: create, update and toggle recorded in order",
              actions[:3] == ["Created role replacement rule",
                              "Updated role replacement rule",
                              "Disabled role replacement rule"], str(actions))
        client.delete(f"/api/role-replacement/rule/{rule_id}",
                      headers={"X-CSRF-Token": CSRF})
        actions = [r[0] for r in rows(
            "SELECT action FROM audit_log WHERE guild_id=? "
            "AND page='role_replacement' ORDER BY id", (GUILD,))]
        check("audit: delete recorded last",
              actions[-1] == "Deleted role replacement rule", str(actions))

    # ── CSRF ────────────────────────────────────────────────────────────
    with patch("dashboard.utils.role_validation.validate_assignable_role",
               assignable_stub):
        for label, token in (("missing", None), ("wrong", "not-the-token")):
            response = post(client, "/api/role-replacement/rule",
                            {"keep_role_id": str(KEEP),
                             "conflict_role_ids": [str(CONFLICT_B)]},
                            csrf=token)
            check(f"csrf: create with a {label} token is refused",
                  response.status_code == 403, str(response.status_code))
        check("csrf: no rule leaked through", rule_count() == 0)

        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": str(KEEP),
                         "conflict_role_ids": [str(CONFLICT_B)]})
        rule_id = (response.get_json() or {}).get("rule", {}).get("id")
        check("csrf: update refuses a missing token",
              client.put(f"/api/role-replacement/rule/{rule_id}",
                         json={"keep_role_id": str(KEEP),
                               "conflict_role_ids": [str(CONFLICT_B)]}
                         ).status_code == 403)
        check("csrf: toggle refuses a missing token",
              client.post(f"/api/role-replacement/rule/{rule_id}/toggle",
                          json={"enabled": False}).status_code == 403)
        check("csrf: delete refuses a missing token",
              client.delete(f"/api/role-replacement/rule/{rule_id}").status_code
              == 403)
        check("csrf: the rule survived every refused write",
              rule_count() == 1
              and rows("SELECT enabled FROM role_replacement_rules WHERE id=?",
                       (rule_id,)) == [(1,)])

    # ── Guild isolation ─────────────────────────────────────────────────
    with patch("dashboard.utils.role_validation.validate_assignable_role",
               assignable_stub):
        other = client_for(guild_id=OTHER_GUILD)
        response = other.get("/api/role-replacement/rules")
        check("guild isolation: another guild sees no rules",
              (response.get_json() or {}).get("rules") == [],
              response.get_data(as_text=True)[:200])
        check("guild isolation: the rule belongs to its own guild only",
              rule_count(GUILD) == 1 and rule_count(OTHER_GUILD) == 0)

        response = post(other, f"/api/role-replacement/rule/{rule_id}/toggle",
                        {"enabled": False})
        check("guild isolation: another guild cannot toggle it",
              response.status_code == 404, str(response.status_code))
        check("guild isolation: another guild cannot delete it",
              other.delete(f"/api/role-replacement/rule/{rule_id}",
                           headers={"X-CSRF-Token": CSRF}).status_code == 404)
        check("guild isolation: still enabled in its own guild",
              rows("SELECT enabled FROM role_replacement_rules WHERE id=?",
                   (rule_id,)) == [(1,)])

        response = post(client, "/api/role-replacement/rule",
                        {"guild_id": str(OTHER_GUILD),
                         "keep_role_id": "5201",
                         "conflict_role_ids": ["5202"]})
        check("guild isolation: a body guild_id is ignored",
              response.status_code == 200
              and rows("SELECT COUNT(*) FROM role_replacement_rules "
                       "WHERE guild_id=?", (OTHER_GUILD,)) == [(0,)],
              response.get_data(as_text=True)[:200])
        check("guild isolation: the rule landed in the session guild",
              rows("SELECT COUNT(*) FROM role_replacement_rules "
                   "WHERE guild_id=? AND keep_role_id=?",
                   (GUILD, 5201)) == [(1,)])
        client.delete(f"/api/role-replacement/rule/"
                      f"{(response.get_json() or {}).get('rule', {}).get('id')}",
                      headers={"X-CSRF-Token": CSRF})

    response = client.get("/api/role-replacement/rules",
                          headers={"X-Guild-ID": str(OTHER_GUILD)})
    check("stale X-Guild-ID is a 409 before the handler runs",
          response.status_code == 409
          and (response.get_json() or {}).get("code") == "GUILD_CONTEXT_MISMATCH",
          str(response.status_code))
    response = client.get("/api/role-replacement/rules",
                          headers={"X-Guild-ID": "not-a-guild"})
    check("malformed X-Guild-ID is a 400",
          response.status_code == 400
          and (response.get_json() or {}).get("code") == "MALFORMED_GUILD_CONTEXT",
          str(response.status_code))

    # ── Role validation ─────────────────────────────────────────────────
    def only_conflicts_unassignable(guild_id, role_id, *_a, **_k):
        # validate_assignable_role(guild_id, role_id, existing=None): the keep
        # role is fine, only the conflicting role is unassignable.
        assert int(guild_id) == GUILD
        if str(role_id) == "5302":
            return False, "Managed by an integration"
        return True, None

    with patch("dashboard.utils.role_validation.validate_assignable_role",
               only_conflicts_unassignable):
        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": "5301",
                         "conflict_role_ids": ["5302"]})
        check("validation: an unassignable conflicting role is refused",
              response.status_code == 400
              and "Invalid conflicting role" in
              (response.get_json() or {}).get("error", ""),
              response.get_data(as_text=True)[:200])
        check("validation: nothing persisted", rule_count() == 1)
    with patch("dashboard.utils.role_validation.validate_assignable_role",
               unassignable_reason("everyone", "@everyone cannot be assigned")):
        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": str(EVERYONE),
                         "conflict_role_ids": ["5302"]})
        check("validation: an @everyone keep role is refused",
              response.status_code == 400
              and "Invalid keep role" in
              (response.get_json() or {}).get("error", ""),
              response.get_data(as_text=True)[:200])
    with patch("dashboard.utils.role_validation.validate_assignable_role",
               unassignable_reason("bot_status_unknown",
                                   "Unable to verify Nero's permissions")):
        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": "5401",
                         "conflict_role_ids": ["5402"]})
        check("validation: an unknown bot state fails closed",
              response.status_code == 400
              and "Unable to verify" in
              (response.get_json() or {}).get("error", ""),
              response.get_data(as_text=True)[:200])
    check("validation: rejected configs never reached the table",
          rule_count() == 1)

    # An unchanged role is not re-validated, so a Discord outage cannot block an
    # unrelated edit (e.g. toggling a rule whose roles are already stored).
    with patch("dashboard.utils.role_validation.validate_assignable_role",
               assignable_stub):
        response = post(client, "/api/role-replacement/rule",
                        {"keep_role_id": "5501",
                         "conflict_role_ids": ["5502"]})
        toggle_target = (response.get_json() or {}).get("rule", {}).get("id")
    with patch("dashboard.utils.role_validation.validate_assignable_role",
               unassignable_reason("bot_status_unknown", "Discord is down")):
        response = client.put(
            f"/api/role-replacement/rule/{toggle_target}",
            json={"keep_role_id": "5501", "conflict_role_ids": ["5502"],
                  "enabled": False},
            headers={"X-CSRF-Token": CSRF})
        check("validation: a save with unchanged roles is not re-validated",
              response.status_code == 200, response.get_data(as_text=True)[:200])
        check("validation: the unchanged save took effect",
              rows("SELECT enabled FROM role_replacement_rules WHERE id=?",
                   (toggle_target,)) == [(0,)])

    # ── Configuration only ──────────────────────────────────────────────
    check("no retroactive cleanup: no pending removal rows after all saves",
          rows("SELECT COUNT(*) FROM role_replacement_pending") == [(0,)])
    check("no retroactive cleanup: no member role rows were written",
          rows("SELECT COUNT(*) FROM equipped_roles") == [(0,)])
    check("no retroactive cleanup: no level claims were touched",
          rows("SELECT COUNT(*) FROM level_reward_claims") == [(0,)])

    # Runtime visibility from the dashboard's point of view: the row written by
    # the API is what the bot's loader reads on its next event.
    loaded = asyncio.run(RR.load_enabled_rules(GUILD))
    check("runtime: the enabled rule is visible to the bot loader",
          KEEP in loaded["keep"] and loaded["keep"][KEEP]["conflict_role_ids"]
          == (CONFLICT_B,), str(loaded))
    check("runtime: the disabled rule is not loaded",
          5501 not in loaded["keep"], str(loaded))

    print(f"\nROLE REPLACEMENT DASHBOARD: "
          f"{'ALL CHECKS PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    for name in FAILS:
        print(f"  FAILED: {name}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
