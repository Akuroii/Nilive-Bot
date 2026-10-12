"""Guild-scoped Shop Publication lifecycle APIs."""
from flask import jsonify, request

from dashboard.api import api_bp
from dashboard.permissions import LEVEL_OWNER, get_session_guild_id, log_action, require_api_permission
from utils.shop_publication import (
    DiscordREST, PublicationError, authorize_replacement_retry, design_exists,
    forget_publication_record, get_publication, list_publications, post_publish,
    recheck_design_publications, unpublish, update_publication,
)


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 and str(value).strip() == str(result) else None


def _service_error(exc):
    return jsonify({"success": False, "code": exc.code, "error": str(exc)}), exc.status


def _validation_response(prepared):
    return jsonify({"success": False, "code": "publication_validation_failed",
                    "error": prepared["blocking_errors"][0]["message"],
                    "problems": prepared["blocking_errors"],
                    "warnings": prepared.get("warnings", [])}), 400


def _ack_response(prepared):
    return jsonify({"success": False, "requires_warning_ack": True,
                    "code": "publication_warnings", "warnings": prepared.get("warnings", []),
                    "warning_token": prepared.get("warning_token"),
                    "message": "Review these warnings and explicitly confirm to continue."}), 200


@api_bp.route("/shop-publisher/designs/<int:design_id>/publications", methods=["GET"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publications_for_design(design_id: int):
    guild_id = get_session_guild_id()
    if not design_exists(guild_id, design_id):
        return jsonify({"success": False, "error": "Design not found."}), 404
    return jsonify({"success": True, "publications": list_publications(guild_id, design_id)})


@api_bp.route("/shop-publisher/publications/publish", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publication_publish():
    guild_id = get_session_guild_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "code": "invalid_body",
                        "error": "Request body must be a JSON object."}), 400
    design_id, channel_id = _positive_id(data.get("design_id")), _positive_id(data.get("channel_id"))
    if design_id is None or channel_id is None:
        return jsonify({"success": False, "code": "invalid_ids",
                        "error": "A valid saved Design and channel are required."}), 400
    try:
        result = post_publish(guild_id, design_id, channel_id,
                              warning_token=data.get("warning_token"), rest=DiscordREST())
    except PublicationError as exc:
        return _service_error(exc)
    if result["outcome"] == "blocked":
        return _validation_response(result["prepared"])
    if result["outcome"] == "ack_required":
        return _ack_response(result["prepared"])
    if result["outcome"] == "published":
        record = result["publication"]
        log_action(guild_id, f"Published Shop Design #{design_id} as Publication #{record['id']}",
                   "shoppublisher", target_id=record["id"], target_name=str(design_id))
        return jsonify({"success": True, "publication": record,
                        "warnings": result.get("warnings", [])}), 201
    if result["outcome"] == "uncertain":
        record = result["publication"]
        log_action(guild_id, f"Shop Publication #{record['id']} send outcome uncertain",
                   "shoppublisher", details=result.get("error"),
                   target_id=record["id"], target_name=str(design_id))
        return jsonify({"success": False, "uncertain": True, "publication": record,
                        "error": result.get("error"),
                        "message": "The send result is uncertain. Do not retry; this Publication is pending attention."}), 202
    record = result["publication"]
    log_action(guild_id, f"Shop Publication #{record['id']} send failed", "shoppublisher",
               details=result.get("error"), target_id=record["id"], target_name=str(design_id))
    return jsonify({"success": False, "publication": record,
                    "error": result.get("error")}), 502


@api_bp.route("/shop-publisher/publications/<int:publication_id>/update", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publication_update(publication_id: int):
    guild_id = get_session_guild_id()
    publication = get_publication(guild_id, publication_id)
    if publication is None:
        return jsonify({"success": False, "error": "Publication not found."}), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "code": "invalid_body",
                        "error": "Request body must be a JSON object."}), 400
    try:
        result = update_publication(guild_id, publication,
                                    warning_token=data.get("warning_token"), rest=DiscordREST())
    except PublicationError as exc:
        return _service_error(exc)
    if result["outcome"] == "blocked":
        return _validation_response(result["prepared"])
    if result["outcome"] == "ack_required":
        return _ack_response(result["prepared"])
    if result["outcome"] == "replacement_uncertain":
        return jsonify({"success": False, "code": "replacement_send_uncertain",
                        "publication": result["publication"], "error": result["error"],
                        "message": "Use the explicit recovery action before another replacement attempt."}), 409
    if result["outcome"] in {"updated", "replaced"}:
        saved = result["publication"]
        action = "updated" if result["outcome"] == "updated" else "replaced missing message for"
        log_action(guild_id, f"{action} Shop Publication #{publication_id}", "shoppublisher",
                   target_id=publication_id, target_name=str(publication["design_id"]))
        return jsonify({"success": True, "publication": saved,
                        "warnings": result.get("warnings", [])})
    saved = result.get("publication")
    log_action(guild_id, f"Shop Publication #{publication_id} update needs attention",
               "shoppublisher", details=result.get("error"), target_id=publication_id,
               target_name=str(publication["design_id"]))
    code = "uncertain_update" if result["outcome"] == "uncertain" else "publication_attention"
    return jsonify({"success": False, "code": code, "publication": saved,
                    "error": result.get("error")}), 202 if result["outcome"] == "uncertain" else 409


@api_bp.route("/shop-publisher/publications/<int:publication_id>/authorize-replacement", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publication_authorize_replacement(publication_id: int):
    guild_id = get_session_guild_id()
    saved = authorize_replacement_retry(guild_id, publication_id)
    if saved is None:
        return jsonify({"success": False, "error": "No uncertain replacement requires authorization."}), 409
    log_action(guild_id, f"Authorized replacement retry for Shop Publication #{publication_id}",
               "shoppublisher", target_id=publication_id, target_name=str(saved["design_id"]))
    return jsonify({"success": True, "publication": saved,
                    "message": "A later Update may now explicitly attempt a replacement."})


@api_bp.route("/shop-publisher/publications/<int:publication_id>", methods=["DELETE"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publication_unpublish(publication_id: int):
    guild_id = get_session_guild_id()
    publication = get_publication(guild_id, publication_id)
    if publication is None:
        return jsonify({"success": False, "error": "Publication not found."}), 404
    try:
        result = unpublish(guild_id, publication, rest=DiscordREST())
    except PublicationError as exc:
        return _service_error(exc)
    if result["outcome"] == "removed":
        log_action(guild_id, f"Unpublished Shop Publication #{publication_id}",
                   "shoppublisher", target_id=publication_id,
                   target_name=str(publication["design_id"]))
        payload = {"success": True, "already_missing": result.get("already_missing", False)}
        if result.get("channel_deleted"):
            payload["channel_deleted"] = True
        return jsonify(payload)
    saved = result.get("publication")
    log_action(guild_id, f"Shop Publication #{publication_id} unpublish needs attention",
               "shoppublisher", details=result.get("error"), target_id=publication_id,
               target_name=str(publication["design_id"]))
    code = "uncertain_unpublish" if result["outcome"] == "uncertain" else "publication_attention"
    return jsonify({"success": False, "code": code, "publication": saved,
                    "error": result.get("error")}), 202 if result["outcome"] == "uncertain" else 409


@api_bp.route("/shop-publisher/designs/<int:design_id>/publications/recheck", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publications_recheck(design_id: int):
    """Ask Discord about this design's stable publications; clear only confirmed-gone ones."""
    guild_id = get_session_guild_id()
    if not design_exists(guild_id, design_id):
        return jsonify({"success": False, "error": "Saved Design was not found in this server."}), 404
    try:
        summary = recheck_design_publications(guild_id, design_id, rest=DiscordREST())
    except PublicationError as exc:
        return _service_error(exc)
    if summary["removed"]:
        log_action(guild_id, f"Re-checked Shop Publications for Design #{design_id}: "
                             f"{summary['removed']} stale record(s) cleared",
                   "shoppublisher", target_id=design_id, target_name=str(design_id))
    return jsonify({"success": True, **summary})


@api_bp.route("/shop-publisher/publications/<int:publication_id>/forget", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
def api_shop_publication_forget(publication_id: int):
    """Operator-confirmed, database-only removal of one stuck association.

    Never contacts Discord: a message may still exist and must be deleted manually.
    """
    guild_id = get_session_guild_id()
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return jsonify({"success": False, "code": "confirmation_required",
                        "error": "Explicit confirmation is required to remove a record only."}), 400
    publication = get_publication(guild_id, publication_id)
    if publication is None:
        return jsonify({"success": False, "error": "Publication not found."}), 404
    if not forget_publication_record(guild_id, publication_id):
        return jsonify({"success": False, "code": "record_not_removable",
                        "error": "This Publication is not in a state that allows record-only removal."}), 409
    log_action(guild_id, f"Removed Shop Publication #{publication_id} record only (Discord untouched)",
               "shoppublisher", details=f"Previous status: {publication.get('status')}",
               target_id=publication_id, target_name=str(publication["design_id"]))
    return jsonify({"success": True, "discord_untouched": True})
