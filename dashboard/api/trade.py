import aiosqlite
from flask import jsonify, request
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.permissions import (
    get_session_guild_id, log_action, require_api_permission, LEVEL_ADMIN,
)
from dashboard.api import api_bp

# ── Trade (dashboard read-only page) ────────────────────────────────────────
#
# Trade System (utils/trade_engine.py, cogs/trade.py) shipped pass #15,
# Discord-side only. This is the optional dashboard follow-up flagged in
# STATUS.md/memory since pass #15 — a read-only history view, same shape
# as dashboard/api/misc.py's /ledger and /inventory routes. Reuses
# utils.trade_engine.get_trade_history() rather than re-declaring the
# query here, same principle as every other "dashboard API wraps an
# existing engine function" route in this project
# (dashboard/api/leveling.py's prestige routes, dashboard/api/minigames.py).
#
# No write routes here on purpose — trades are only ever created through
# the /trade Discord UI's atomic execute_trade() (live balance/inventory
# re-validation happens inside that transaction). A dashboard "create/edit
# trade" surface would need to reimplement that same re-validation to be
# safe, which is out of scope for what Dark asked for (history view only).


@api_bp.route("/trade/history")
@require_api_permission(LEVEL_ADMIN)
def api_trade_history():
    guild_id = get_session_guild_id()
    user_id  = request.args.get("user_id")
    limit    = min(int(request.args.get("limit", 100)), 500)

    async def fetch():
        from utils.trade_engine import get_trade_history
        uid = int(user_id) if user_id else None
        return await get_trade_history(guild_id, uid, limit=limit)

    rows = run_async(fetch())
    return jsonify({"trades": rows, "guild_id": guild_id})
