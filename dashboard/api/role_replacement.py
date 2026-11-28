"""Dashboard API for Role Replacement (keep role vs conflicting roles).

Every route is guild-scoped from the session (``get_session_guild_id``); no
request body or query string can name another guild. Authentication, CSRF and
stale-context rejection are enforced by the shared ``/api`` blueprint and
``dashboard/app.py`` before_request hooks, so they are not re-implemented here.

Role validation goes through the same ``validate_assignable_role`` gate every
other role configuration surface uses (existence, @everyone, managed roles, bot
permission and hierarchy, tri-state bot status). It is delta-aware: a role a
rule already stores is re-validated only when it changed, so a transient
Discord outage cannot block an unrelated edit such as a toggle.
"""
from flask import jsonify, request

from dashboard.api import api_bp
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_ADMIN,
)
from dashboard.utils.async_utils import run_async
from utils.role_replacement import (
    RoleReplacementError, _normalize_role_list, delete_rule, list_rules,
    normalize_role_id, save_rule, set_rule_enabled,
)

PAGE = "role_replacement"


def _rule_payload(rule: dict) -> dict:
    return {
        "id": rule["id"],
        "keep_role_id": str(rule["keep_role_id"]),
        "conflict_role_ids": [str(r) for r in rule["conflict_role_ids"]],
        "enabled": bool(rule["enabled"]),
    }


def _find_rule(guild_id: int, rule_id: int) -> dict | None:
    for rule in run_async(list_rules(guild_id)):
        if rule["id"] == int(rule_id):
            return rule
    return None


def _validate_roles(guild_id: int, keep: int, conflicts: list[int],
                    current: dict | None) -> str | None:
    """Same assignability gate as the other role config surfaces.

    A role already stored on this rule is accepted unchanged; anything new (or
    changed) must be assignable right now, and an unknown bot state fails
    closed with the validator's own message.
    """
    from dashboard.utils.role_validation import validate_assignable_role

    current_keep = str((current or {}).get("keep_role_id") or "")
    current_conflicts = set(
        str(r) for r in (current or {}).get("conflict_role_ids") or ())

    if str(keep) != current_keep:
        valid, err = validate_assignable_role(guild_id, str(keep))
        if not valid:
            return f"Invalid keep role: {err}"
    for role_id in conflicts:
        if str(role_id) in current_conflicts:
            continue
        valid, err = validate_assignable_role(guild_id, str(role_id))
        if not valid:
            return f"Invalid conflicting role {role_id}: {err}"
    return None


def _parse(payload: dict, *, current: dict | None):
    """Normalise the request into ``(keep, conflicts, enabled)``.

    A field the caller omitted keeps the current value, so a toggle request
    does not have to resend the roles.
    """
    keep_raw = payload.get("keep_role_id",
                           (current or {}).get("keep_role_id"))
    conflicts_raw = payload.get("conflict_role_ids",
                                (current or {}).get("conflict_role_ids"))
    keep = normalize_role_id(keep_raw)
    conflicts = _normalize_role_list(conflicts_raw, label="Conflicting roles")
    enabled = int(bool(payload.get(
        "enabled", (current or {}).get("enabled", True))))
    return keep, conflicts, enabled


@api_bp.route("/role-replacement/rules", methods=["GET"])
@require_api_permission(LEVEL_ADMIN)
def get_role_replacement_rules():
    guild_id = get_session_guild_id()
    rules = run_async(list_rules(guild_id))
    return jsonify({"success": True, "rules": [_rule_payload(r) for r in rules]})


@api_bp.route("/role-replacement/rule", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def create_role_replacement_rule():
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Malformed request"}), 400

    try:
        keep, conflicts, enabled = _parse(data, current=None)
    except RoleReplacementError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    error = _validate_roles(guild_id, keep, conflicts, None)
    if error:
        return jsonify({"success": False, "error": error}), 400

    try:
        result = run_async(save_rule(guild_id, keep, conflicts, enabled=enabled))
    except RoleReplacementError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    log_action(guild_id, "Created role replacement rule", PAGE,
               details=(f"keep={result['keep_role_id']} "
                        f"conflicts={result['conflict_role_ids']}"))
    return jsonify({"success": True, "rule": _rule_payload(result)})


@api_bp.route("/role-replacement/rule/<int:rule_id>", methods=["PUT"])
@require_api_permission(LEVEL_ADMIN)
def update_role_replacement_rule(rule_id: int):
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Malformed request"}), 400

    current = _find_rule(guild_id, rule_id)
    if current is None:
        return jsonify({"success": False, "error": "Rule not found"}), 404

    try:
        keep, conflicts, enabled = _parse(data, current=current)
    except RoleReplacementError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    error = _validate_roles(guild_id, keep, conflicts, current)
    if error:
        return jsonify({"success": False, "error": error}), 400

    try:
        result = run_async(save_rule(
            guild_id, keep, conflicts, rule_id=int(rule_id), enabled=enabled))
    except RoleReplacementError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    log_action(guild_id, "Updated role replacement rule", PAGE,
               details=(f"keep={result['keep_role_id']} "
                        f"conflicts={result['conflict_role_ids']} "
                        f"enabled={int(result['enabled'])}"))
    return jsonify({"success": True, "rule": _rule_payload(result)})


@api_bp.route("/role-replacement/rule/<int:rule_id>", methods=["DELETE"])
@require_api_permission(LEVEL_ADMIN)
def delete_role_replacement_rule(rule_id: int):
    guild_id = get_session_guild_id()
    deleted = run_async(delete_rule(guild_id, int(rule_id)))
    if not deleted:
        return jsonify({"success": False, "error": "Rule not found"}), 404
    log_action(guild_id, "Deleted role replacement rule", PAGE,
               details=f"rule={int(rule_id)}")
    return jsonify({"success": True})


@api_bp.route("/role-replacement/rule/<int:rule_id>/toggle", methods=["POST"])
@require_api_permission(LEVEL_ADMIN)
def toggle_role_replacement_rule(rule_id: int):
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if data is not None and not isinstance(data, dict):
        return jsonify({"success": False, "error": "Malformed request"}), 400
    enabled = bool((data or {}).get("enabled", True))
    changed = run_async(set_rule_enabled(guild_id, int(rule_id), enabled))
    if not changed:
        return jsonify({"success": False, "error": "Rule not found"}), 404
    log_action(guild_id,
               "Enabled role replacement rule" if enabled
               else "Disabled role replacement rule",
               PAGE, details=f"rule={int(rule_id)}")
    return jsonify({"success": True, "enabled": enabled})
