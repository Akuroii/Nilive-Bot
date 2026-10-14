import os
import time
import threading
from flask import jsonify
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_OWNER,
    require_bot_owner_api,
)
from dashboard.api import api_bp

# ── Backups (manual trigger) ────────────────────────────────────────────────
#
# BUILD (backup visibility): dashboard/app.py's /backups route handles the
# read side (list + stats). This is the one write action — a manual
# "trigger backup now" button.
#
# Wraps cogs/backup.py's existing _do_backup() rather than reimplementing
# backup creation here — same "dashboard API wraps an existing engine
# function" principle as dashboard/api/trade.py (get_trade_history) and
# dashboard/api/minigames.py. This is now the only other caller of
# _do_backup() besides the daily @tasks.loop and the /backup_now Discord
# command — uuid-suffixed filenames, the backup_log insert, and pruning to
# KEEP_BACKUPS all stay defined in exactly one place.
#
# Lazy import (inside trigger(), not at module top) matches the existing
# convention for engine-module imports elsewhere in this package (e.g.
# dashboard/api/trade.py's get_trade_history, resolve_users) rather than
# module-level, and also keeps this file import-safe even if cogs.backup
# ever grew a heavier top-level dependency.
#
# Wrapped in try/except here — unlike most POST routes in this package
# (e.g. mvp.py's save_mvp_config_api, which just lets exceptions bubble) —
# because _do_backup()'s own author already treated it as fallible:
# cogs/backup.py's /backup_now Discord command wraps the same call in
# try/except. Mirroring that judgment call, not inventing new caution.
#
# SECURITY CHECKPOINT (this pass), two fixes:
#
# 1) require_bot_owner_api added below, on top of the pre-existing
#    require_api_permission(LEVEL_OWNER). Confirmed live before this fix
#    (two-guild dashboard_users test fixture): a LEVEL_OWNER in ANY
#    guild — not just the real bot owner's — could read AND trigger
#    backups of the ENTIRE bot, every guild's data, because
#    require_api_permission only checks LEVEL_OWNER *within whichever
#    guild is selected in the caller's own session*, and backups aren't
#    guild data. require_bot_owner_api (dashboard/permissions.py) closes
#    this with a guild_id-blind check instead. See that function's
#    docstring for the full explanation; not duplicated here.
#
# 2) Rate limiting added below (_try_reserve_backup_slot /
#    _release_backup_slot). The UI button disabling itself was never
#    real protection — a direct POST to this URL, or two different
#    LEVEL_OWNER sessions, could still fire back-to-back. Single global
#    in-memory cooldown (not per-user — there's exactly one backup
#    operation here, not one per caller), same time.time() / "now - last
#    < cooldown" idiom as main.py's _command_cooldowns and
#    cogs/economy.py's _daily_cooldowns/_work_cooldowns, just without
#    their per-key dict (nothing to key on: this is one global resource).
#    Lock-guarded reserve-then-commit-or-release so two near-simultaneous
#    requests can't both slip through, and a FAILED backup attempt
#    doesn't spend the cooldown (only a successful one does).

BACKUP_TRIGGER_COOLDOWN_SECONDS = int(os.getenv("BACKUP_TRIGGER_COOLDOWN_SECONDS", "300"))

_backup_trigger_lock = threading.Lock()
_last_backup_trigger_at = 0.0


def _try_reserve_backup_slot():
    """Atomically checks the cooldown and reserves it if allowed.
    Returns (True, previous_timestamp) on success — the caller MUST call
    _release_backup_slot(previous_timestamp) if the backup attempt then
    fails, so a failed attempt doesn't cost the cooldown. Returns
    (False, seconds_remaining) if still cooling down.

    Resets to 0 on dashboard process restart (in-memory only, no DB
    table) — deliberate: this cooldown is about spam through THIS
    endpoint specifically, not "backups in general", so a redeploy or
    the daily automated backup (which runs in the separate bot process
    and never touches this endpoint) correctly doesn't count against it.

    Lock isn't exploitable today — dashboard/app.py's app.run() doesn't
    pass threaded=True, so requests are handled one at a time under the
    Flask dev server it currently runs on — but costs nothing and stays
    correct if that changes (threaded=True, or a real multi-worker WSGI
    server later).
    """
    global _last_backup_trigger_at
    with _backup_trigger_lock:
        now = time.time()
        elapsed = now - _last_backup_trigger_at
        if elapsed < BACKUP_TRIGGER_COOLDOWN_SECONDS:
            return False, round(BACKUP_TRIGGER_COOLDOWN_SECONDS - elapsed, 1)
        previous = _last_backup_trigger_at
        _last_backup_trigger_at = now
        return True, previous


def _release_backup_slot(previous_timestamp: float):
    global _last_backup_trigger_at
    with _backup_trigger_lock:
        _last_backup_trigger_at = previous_timestamp


@api_bp.route("/backups/trigger", methods=["POST"])
@require_api_permission(LEVEL_OWNER)
@require_bot_owner_api
def api_trigger_backup():
    guild_id = get_session_guild_id()

    allowed, info = _try_reserve_backup_slot()
    if not allowed:
        return jsonify({
            "success": False,
            "error": f"Slow down — try again in {info}s.",
        }), 429

    async def trigger():
        from cogs.backup import _do_backup
        return await _do_backup()

    try:
        result = run_async(trigger())
    except Exception as e:
        _release_backup_slot(info)
        return jsonify({"success": False, "error": str(e)}), 500

    log_action(guild_id, f"Triggered manual backup ({result['filename']})", "backups")
    return jsonify({
        "success":    True,
        "filename":   result["filename"],
        "size_bytes": result["size_bytes"],
    })
