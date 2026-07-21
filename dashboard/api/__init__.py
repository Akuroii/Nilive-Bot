from flask import Blueprint, jsonify, request, session

# REFACTOR (dark-fixes pass #3): dashboard/api.py was a single 2159-line
# file with 90+ routes. Split into this package by the section headers
# the original file already had (moderation, tickets, mvp, economy/shop,
# leveling, misc) — those divisions were already there in the comments,
# this just makes them real file boundaries.
#
# Deliberately kept as ONE shared Blueprint object that every submodule
# imports and registers routes onto (rather than N blueprints each with
# their own url_prefix) — that's what makes this a zero-risk split:
# dashboard/app.py's `from dashboard.api import api_bp` /
# `app.register_blueprint(api_bp)` doesn't change at all, and every
# route keeps its exact existing URL, method, and endpoint name. This
# was verified directly: captured all 93 /api/* routes (path, methods,
# endpoint name) from the app before the split and diffed against the
# same capture after — exact match, not just "looks right".
#
# Each submodule currently repeats the full original import block
# rather than a pruned per-file one — slightly more than strictly
# needed, but zero risk of a missing import silently breaking a route
# that used to work. Pruning unused imports per-file is a safe,
# separate future cleanup if wanted.
#
# dark-fixes pass #13: added dashboard.api.minigames — the dashboard
# CRUD surface for the Event Stack Builder (cogs/minigames.py), which
# was previously Discord-command-only. Same registration pattern as
# every other submodule below: import it, its @api_bp.route(...)
# decorators register onto the shared blueprint, no other file needs
# to change.
#
# Phase 6 (Missions, built ahead of the Trade-verification gate — see
# utils/mission_engine.py header): added dashboard.api.missions, same
# registration pattern.

api_bp = Blueprint("api", __name__, url_prefix="/api")

CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@api_bp.before_request
def _enforce_csrf():
    if request.method in CSRF_SAFE_METHODS:
        return
    session_token = session.get("csrf_token")
    header_token   = request.headers.get("X-CSRF-Token", "")
    if not session_token or not header_token or header_token != session_token:
        return jsonify({
            "success": False,
            "error": "CSRF validation failed. Refresh the page and try again.",
        }), 403


# Importing each submodule executes its @api_bp.route(...) decorators,
# registering its routes onto the single api_bp object above. Order
# doesn't matter for correctness (no route path collisions across
# sections), only listed alphabetically for readability.
from dashboard.api import core          # noqa: E402,F401
from dashboard.api import economy_shop  # noqa: E402,F401
from dashboard.api import leveling      # noqa: E402,F401
from dashboard.api import minigames     # noqa: E402,F401
from dashboard.api import missions      # noqa: E402,F401
from dashboard.api import misc          # noqa: E402,F401
from dashboard.api import moderation    # noqa: E402,F401
from dashboard.api import mvp           # noqa: E402,F401
from dashboard.api import tickets       # noqa: E402,F401
