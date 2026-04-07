import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import math
import aiosqlite
from flask import (
    Flask, redirect, url_for, session,
    request, render_template, jsonify, abort,
)
from database import DB_PATH
from dashboard.utils.async_utils import run_async
from dashboard.auth import (
    login_required, create_session, clear_session,
    get_discord_oauth_url, exchange_code, fetch_discord_user,
    fetch_discord_guilds, current_user, current_user_id,
)
from dashboard.permissions import (
    require_page, get_current_user_context, log_action,
    get_session_guild_id, set_session_guild,
    LEVEL_RANK, LEVEL_OWNER,
)
from dashboard.api import api_bp
from utils.xp_calculator import calculate_level_from_xp

app = Flask(__name__,
            template_folder="templates",
            static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "nero-dashboard-secret-key-2024")
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7
app.register_blueprint(api_bp)


def render(template, **ctx):
    return render_template(template, **ctx)


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("errors/403.html"), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("errors/404.html"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("errors/500.html"), 500


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("server_select"))
    return render_template("login.html")


@app.route("/discord_login")
def discord_login():
    return redirect(get_discord_oauth_url())


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect(url_for("login"))
    tokens = exchange_code(code)
    if not tokens or not tokens.get("access_token"):
        return redirect(url_for("login"))
    user = fetch_discord_user(tokens["access_token"])
    if not user:
        return redirect(url_for("login"))
    remember = request.args.get("remember") == "1"
    create_session(user, remember_me=remember)
    session["access_token"] = tokens["access_token"]
    return redirect(url_for("server_select"))


@app.route("/logout")
def logout():
    clear_session()
    return redirect(url_for("login"))


# ── Server select ──────────────────────────────────────────────────────────────

@app.route("/server-select")
@login_required
def server_select():
    access_token = session.get("access_token", "")
    guilds       = fetch_discord_guilds(access_token) if access_token else []

    async def get_accessible_guilds():
        if not guilds:
            return []
        user_id    = current_user_id()
        accessible = []
        async with aiosqlite.connect(DB_PATH) as db:
            for gid in [int(g["id"]) for g in guilds]:
                cursor = await db.execute("""
                    SELECT permission_level FROM dashboard_users
                    WHERE guild_id = ? AND user_id = ? AND enabled = 1
                """, (gid, user_id))
                row = await cursor.fetchone()
                if row:
                    guild_data = next(
                        (g for g in guilds if int(g["id"]) == gid), None)
                    if guild_data:
                        accessible.append({
                            "id":    gid,
                            "name":  guild_data["name"],
                            "icon":  guild_data.get("icon"),
                            "level": row[0],
                        })
        return accessible

    accessible = run_async(get_accessible_guilds())
    return render_template("server_select.html",
                           user=current_user(), guilds=accessible)


@app.route("/select-guild/<int:guild_id>")
@login_required
def select_guild(guild_id: int):
    user_id = current_user_id()

    async def check():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT permission_level FROM dashboard_users
                WHERE guild_id = ? AND user_id = ? AND enabled = 1
            """, (guild_id, user_id))
            return await cursor.fetchone()

    row = run_async(check())
    if not row:
        abort(403)

    set_session_guild(guild_id)
    session["user_level"] = row[0]

    access_token = session.get("access_token", "")
    if access_token:
        guilds = fetch_discord_guilds(access_token)
        gdata  = next((g for g in guilds if int(g["id"]) == guild_id), None)
        session["guild_name"] = gdata["name"] if gdata else ""

    return redirect(url_for("index"))


# ── Overview ───────────────────────────────────────────────────────────────────

@app.route("/")
@require_page("overview")
def index():
    guild_id = get_session_guild_id()

    async def get_stats():
        async with aiosqlite.connect(DB_PATH) as db:
            mvp_count    = (await (await db.execute(
                "SELECT COUNT(*) FROM mvp_scores WHERE guild_id=?",
                (guild_id,))).fetchone())[0]
            member_count = (await (await db.execute(
                "SELECT COUNT(*) FROM levels WHERE guild_id=?",
                (guild_id,))).fetchone())[0]
            open_tickets = (await (await db.execute(
                "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='open'",
                (guild_id,))).fetchone())[0]
            warn_count   = (await (await db.execute(
                "SELECT COUNT(*) FROM warnings WHERE guild_id=?",
                (guild_id,))).fetchone())[0]
        return {
            "mvp_count":    mvp_count,
            "member_count": member_count,
            "open_tickets": open_tickets,
            "warn_count":   warn_count,
        }

    stats = run_async(get_stats())
    ctx   = get_current_user_context()
    return render("general/overview.html", stats=stats, **ctx)


# ── Members ────────────────────────────────────────────────────────────────────

@app.route("/members")
@require_page("members_view")
def members():
    guild_id = get_session_guild_id()

    async def get_members():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT l.user_id, l.xp, l.level,
                       COALESCE(e.balance, 0) AS coins
                FROM levels l
                LEFT JOIN economy e
                  ON l.user_id = e.user_id AND l.guild_id = e.guild_id
                WHERE l.guild_id = ?
                ORDER BY l.xp DESC LIMIT 100
            """, (guild_id,))
            rows = await cursor.fetchall()
        return [{"user_id": r[0], "xp": r[1],
                 "level": r[2], "coins": r[3]} for r in rows]

    member_list = run_async(get_members())
    ctx         = get_current_user_context()
    return render("general/members.html", members=member_list, **ctx)


@app.route("/members/<int:user_id>")
@require_page("members_view")
def member_profile(user_id: int):
    guild_id = get_session_guild_id()

    async def get_profile():
        async with aiosqlite.connect(DB_PATH) as db:
            lc        = await db.execute(
                "SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            level_row = await lc.fetchone()

            ec       = await db.execute(
                "SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id))
            econ_row = await ec.fetchone()

            wc = await db.execute("""
                SELECT reason, timestamp, moderator_display_name
                FROM warnings
                WHERE guild_id=? AND user_id=?
                ORDER BY timestamp DESC LIMIT 10
            """, (guild_id, user_id))
            warnings = await wc.fetchall()

            mc = await db.execute("""
                SELECT action, reason, moderator_display_name,
                       created_at, source
                FROM moderation_logs
                WHERE guild_id=? AND user_id=? AND deleted=0
                ORDER BY created_at DESC LIMIT 10
            """, (guild_id, user_id))
            mod_logs = await mc.fetchall()

            pc = await db.execute("""
                SELECT item_name, price_paid, purchased_at
                FROM purchase_history
                WHERE guild_id=? AND user_id=?
                ORDER BY purchased_at DESC LIMIT 10
            """, (guild_id, user_id))
            purchases = await pc.fetchall()

        return {
            "xp":       level_row[0] if level_row else 0,
            "level":    level_row[1] if level_row else 0,
            "coins":    econ_row[0]  if econ_row  else 0,
            "warnings": warnings,
            "mod_logs": mod_logs,
            "purchases": purchases,
        }

    profile = run_async(get_profile())
    ctx     = get_current_user_context()
    return render("general/member_profile.html",
                  profile=profile, member_id=user_id, **ctx)


# ── Audit log ──────────────────────────────────────────────────────────────────

@app.route("/audit-log")
@require_page("audit_log")
def audit_log():
    guild_id = get_session_guild_id()

    async def get_logs():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, user_id, user_display_name, target_id,
                       target_name, action, details, page, created_at
                FROM audit_log
                WHERE guild_id = ?
                ORDER BY created_at DESC LIMIT 200
            """, (guild_id,))
            return await cursor.fetchall()

    logs = run_async(get_logs())
    ctx  = get_current_user_context()
    return render("general/auditlog.html", logs=logs, **ctx)


# ── Moderation ─────────────────────────────────────────────────────────────────

@app.route("/moderation")
@require_page("moderation_view")
def moderation():
    guild_id = get_session_guild_id()
    tab      = request.args.get("tab", "logs")
    page     = int(request.args.get("page", 1))
    per_page = 50

    async def get_data():
        async with aiosqlite.connect(DB_PATH) as db:
            action_filter = request.args.get("action", "")
            mod_filter    = request.args.get("moderator", "")
            search        = request.args.get("search", "")
            date_from     = request.args.get("date_from", "")
            date_to       = request.args.get("date_to", "")

            where  = ["guild_id = ?", "deleted = 0"]
            params = [guild_id]
            if action_filter:
                where.append("action = ?"); params.append(action_filter)
            if mod_filter:
                where.append("moderator_id = ?"); params.append(int(mod_filter))
            if search:
                where.append(
                    "(user_display_name LIKE ? OR CAST(user_id AS TEXT) LIKE ?)")
                params += [f"%{search}%", f"%{search}%"]
            if date_from:
                where.append("created_at >= ?"); params.append(date_from)
            if date_to:
                where.append("created_at <= ?"); params.append(date_to + " 23:59:59")

            where_sql = " AND ".join(where)

            total = (await (await db.execute(
                f"SELECT COUNT(*) FROM moderation_logs WHERE {where_sql}",
                params)).fetchone())[0]

            log_cur = await db.execute(f"""
                SELECT id, user_id, user_display_name, user_avatar_url,
                       moderator_id, moderator_display_name,
                       action, reason, source, evidence_url, created_at
                FROM moderation_logs
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, params + [per_page, (page - 1) * per_page])
            logs = await log_cur.fetchall()

            active_cur = await db.execute("""
                SELECT id, user_id, user_display_name, action, reason,
                       expires_at, moderator_display_name
                FROM moderation_logs
                WHERE guild_id=? AND deleted=0
                  AND action IN ('timeout','temp_ban')
                  AND (expires_at IS NULL OR expires_at > datetime('now'))
                ORDER BY created_at DESC
            """, (guild_id,))
            active_punishments = await active_cur.fetchall()

            warn_cur = await db.execute("""
                SELECT user_id, user_display_name,
                       COUNT(*) as warn_count,
                       MAX(reason) as last_reason,
                       MAX(timestamp) as last_warn
                FROM warnings WHERE guild_id=?
                GROUP BY user_id ORDER BY warn_count DESC LIMIT 50
            """, (guild_id,))
            active_warnings = await warn_cur.fetchall()

            thresh_cur = await db.execute("""
                SELECT id, warn_count, action, duration_minutes,
                       role_id, enabled
                FROM warning_thresholds WHERE guild_id=?
                ORDER BY warn_count ASC
            """, (guild_id,))
            thresholds = await thresh_cur.fetchall()

            ae_cur = await db.execute("""
                SELECT value FROM guild_settings_kv
                WHERE guild_id=? AND key='auto_escalation_enabled'
            """, (guild_id,))
            ae_row          = await ae_cur.fetchone()
            auto_escalation = ae_row[0] if ae_row else "1"

            act_cur = await db.execute("""
                SELECT DISTINCT action FROM moderation_logs
                WHERE guild_id=? AND deleted=0
            """, (guild_id,))
            distinct_actions = [r[0] for r in await act_cur.fetchall()]

        return {
            "logs": logs, "total": total,
            "page": page, "per_page": per_page,
            "total_pages": math.ceil(total / per_page) if total else 1,
            "active_punishments": active_punishments,
            "active_warnings":    active_warnings,
            "thresholds":         thresholds,
            "auto_escalation":    auto_escalation,
            "distinct_actions":   distinct_actions,
        }

    data = run_async(get_data())
    ctx  = get_current_user_context()
    return render("manage/moderation.html", tab=tab, **data, **ctx)


# ── Tickets ────────────────────────────────────────────────────────────────────

@app.route("/tickets")
@require_page("tickets")
def tickets():
    guild_id = get_session_guild_id()
    tab      = request.args.get("tab", "general")

    async def get_data():
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                gs_cur = await db.execute(
                    "SELECT * FROM ticket_settings WHERE guild_id=?", (guild_id,))
                gs_row  = await gs_cur.fetchone()
                general = dict(zip(
                    [d[0] for d in gs_cur.description], gs_row)) if gs_row else {}
            except Exception:
                general = {}

            try:
                cat_cur = await db.execute("""
                    SELECT id, name, emoji, viewer_roles, closer_roles,
                           auto_assign_roles, open_embed, enabled, sort_order
                    FROM ticket_categories WHERE guild_id=? ORDER BY sort_order ASC
                """, (guild_id,))
                categories = await cat_cur.fetchall()
            except Exception:
                categories = []

            try:
                panel_cur = await db.execute("""
                    SELECT id, name, channel_id, embed_data, buttons, created_at
                    FROM ticket_panels WHERE guild_id=? ORDER BY id DESC
                """, (guild_id,))
                panels = await panel_cur.fetchall()
            except Exception:
                panels = []

            try:
                t_cur = await db.execute("""
                    SELECT id, channel_id, user_id, status, category,
                           claimed_by, tags, created_at
                    FROM tickets WHERE guild_id=?
                    ORDER BY created_at DESC LIMIT 100
                """, (guild_id,))
                ticket_list = await t_cur.fetchall()
            except Exception:
                ticket_list = []

            try:
                rating_cur  = await db.execute("""
                    SELECT AVG(rating), COUNT(*) FROM ticket_ratings WHERE guild_id=?
                """, (guild_id,))
                rating_row  = await rating_cur.fetchone()
                avg_rating  = round(rating_row[0], 1) if rating_row and rating_row[0] else None
                rating_count = rating_row[1] if rating_row else 0
            except Exception:
                avg_rating   = None
                rating_count = 0

        return {
            "general":      general,
            "categories":   categories,
            "panels":       panels,
            "ticket_list":  ticket_list,
            "avg_rating":   avg_rating,
            "rating_count": rating_count,
        }

    data = run_async(get_data())
    ctx  = get_current_user_context()
    return render("manage/tickets.html", tab=tab, **data, **ctx)


# ── Embed builder ──────────────────────────────────────────────────────────────

@app.route("/embed-builder")
@require_page("embedbuilder")
def embed_builder():
    ctx = get_current_user_context()
    return render("manage/embedbuilder.html", **ctx)


# ── Reaction roles ─────────────────────────────────────────────────────────────

@app.route("/reaction-roles")
@require_page("reactionroles")
def reaction_roles():
    ctx = get_current_user_context()
    return render("manage/reactionroles.html", **ctx)


# ── Triggers ───────────────────────────────────────────────────────────────────

@app.route("/triggers")
@require_page("triggers")
def triggers():
    guild_id = get_session_guild_id()

    async def get_triggers():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, trigger_words, response_type, match_type, enabled
                FROM triggers WHERE guild_id=?
                ORDER BY id DESC
            """, (guild_id,))
            return await cursor.fetchall()

    trigger_list = run_async(get_triggers())
    ctx          = get_current_user_context()
    return render("manage/triggers.html", triggers=trigger_list, **ctx)


# ── Custom commands ────────────────────────────────────────────────────────────

@app.route("/custom-commands")
@require_page("customcommands")
def custom_commands():
    guild_id = get_session_guild_id()

    async def get_commands():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, trigger, actions, enabled, created_at
                FROM custom_commands WHERE guild_id=?
                ORDER BY id DESC
            """, (guild_id,))
            return await cursor.fetchall()

    cmds = run_async(get_commands())
    ctx  = get_current_user_context()
    return render("manage/customcommands.html", commands=cmds, **ctx)


# ── MVP ────────────────────────────────────────────────────────────────────────

@app.route("/mvp")
@require_page("mvp")
def mvp():
    from datetime import date
    guild_id = get_session_guild_id()
    today    = date.today().isoformat()

    async def get_data():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, message_score, voice_minutes, total_score
                FROM mvp_scores WHERE guild_id=? AND date=?
                ORDER BY total_score DESC LIMIT 20
            """, (guild_id, today))
            scores = await cursor.fetchall()
            hist_cursor = await db.execute("""
                SELECT user_id, user_display_name, score,
                       cycle_start, cycle_end
                FROM mvp_history WHERE guild_id=?
                ORDER BY created_at DESC LIMIT 20
            """, (guild_id,))
            history = await hist_cursor.fetchall()
        return scores, history

    scores, history = run_async(get_data())
    ctx             = get_current_user_context()
    return render("systems/mvp.html", scores=scores, history=history, **ctx)


# ── Leveling ───────────────────────────────────────────────────────────────────

@app.route("/leveling")
@require_page("leveling")
def leveling():
    guild_id = get_session_guild_id()

    async def get_data():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level FROM levels
                WHERE guild_id=? ORDER BY xp DESC LIMIT 50
            """, (guild_id,))
            levels = await cursor.fetchall()
            rewards_cursor = await db.execute("""
                SELECT id, level, role_id FROM leveling_rewards
                WHERE guild_id=? ORDER BY level ASC
            """, (guild_id,))
            rewards = await rewards_cursor.fetchall()
        return levels, rewards

    levels, rewards = run_async(get_data())
    ctx             = get_current_user_context()
    return render("systems/leveling.html", levels=levels, rewards=rewards, **ctx)


# ── Economy ────────────────────────────────────────────────────────────────────

@app.route("/economy")
@require_page("economy")
def economy():
    guild_id = get_session_guild_id()

    async def get_data():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, balance FROM economy
                WHERE guild_id=? ORDER BY balance DESC LIMIT 50
            """, (guild_id,))
            return await cursor.fetchall()

    balances = run_async(get_data())
    ctx      = get_current_user_context()
    return render("systems/economy.html", balances=balances, **ctx)


# ── Shop ───────────────────────────────────────────────────────────────────────

@app.route("/shop")
@require_page("shop")
def shop():
    guild_id = get_session_guild_id()

    async def get_items():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, name, description, price, type,
                       role_id, duration_hours, featured, enabled
                FROM shop_items WHERE guild_id=?
                ORDER BY featured DESC, created_at DESC
            """, (guild_id,))
            return await cursor.fetchall()

    items = run_async(get_items())
    ctx   = get_current_user_context()
    return render("systems/shop.html", items=items, **ctx)


# ── Events ─────────────────────────────────────────────────────────────────────

@app.route("/events")
@require_page("events")
def events():
    guild_id = get_session_guild_id()

    async def get_events():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, title, type, reward_type, reward_value,
                       max_winners, enabled, created_at
                FROM events WHERE guild_id=?
                ORDER BY created_at DESC
            """, (guild_id,))
            return await cursor.fetchall()

    event_list = run_async(get_events())
    ctx        = get_current_user_context()
    return render("systems/events.html", events=event_list, **ctx)


# ── Config: General ────────────────────────────────────────────────────────────

@app.route("/config/general", methods=["GET", "POST"])
@require_page("general_settings")
def config_general():
    guild_id = get_session_guild_id()

    async def get_settings():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
            row = await cursor.fetchone()
            if row:
                return dict(zip([d[0] for d in cursor.description], row))
        return {}

    async def save_settings(data: dict):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO guild_settings
                    (guild_id, prefix, timezone, language,
                     log_channel_id, currency_name, currency_emoji_id,
                     status_rotation_enabled, status_rotation_interval)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    prefix                   = excluded.prefix,
                    timezone                 = excluded.timezone,
                    language                 = excluded.language,
                    log_channel_id           = excluded.log_channel_id,
                    currency_name            = excluded.currency_name,
                    currency_emoji_id        = excluded.currency_emoji_id,
                    status_rotation_enabled  = excluded.status_rotation_enabled,
                    status_rotation_interval = excluded.status_rotation_interval,
                    updated_at               = CURRENT_TIMESTAMP
            """, (
                guild_id,
                data.get("prefix", "/"),
                data.get("timezone", "UTC"),
                data.get("language", "en"),
                data.get("log_channel_id") or None,
                data.get("currency_name", "Coins"),
                data.get("currency_emoji_id") or None,
                int(bool(data.get("status_rotation_enabled"))),
                int(data.get("status_rotation_interval", 5)),
            ))
            await db.commit()

    if request.method == "POST":
        run_async(save_settings(request.form.to_dict()))
        log_action(guild_id, "Updated general settings", "config_general")
        return redirect(url_for("config_general") + "?saved=1")

    settings = run_async(get_settings())
    ctx      = get_current_user_context()
    return render("config/general.html",
                  settings=settings, saved=request.args.get("saved"), **ctx)


# ── Config: Welcome ────────────────────────────────────────────────────────────

@app.route("/config/welcome", methods=["GET", "POST"])
@require_page("welcome")
def config_welcome():
    guild_id = get_session_guild_id()

    async def get_config():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM welcome_config WHERE guild_id=?", (guild_id,))
            row = await cursor.fetchone()
            if row:
                return dict(zip([d[0] for d in cursor.description], row))
        return {}

    async def save_config(data: dict):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO welcome_config
                    (guild_id, join_enabled, join_channel_id, auto_role_id,
                     join_message_mode, leave_enabled, leave_channel_id,
                     rules_enabled, rules_channel_id, rules_role_id,
                     rules_button_text)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    join_enabled      = excluded.join_enabled,
                    join_channel_id   = excluded.join_channel_id,
                    auto_role_id      = excluded.auto_role_id,
                    join_message_mode = excluded.join_message_mode,
                    leave_enabled     = excluded.leave_enabled,
                    leave_channel_id  = excluded.leave_channel_id,
                    rules_enabled     = excluded.rules_enabled,
                    rules_channel_id  = excluded.rules_channel_id,
                    rules_role_id     = excluded.rules_role_id,
                    rules_button_text = excluded.rules_button_text,
                    updated_at        = CURRENT_TIMESTAMP
            """, (
                guild_id,
                int(bool(data.get("join_enabled"))),
                data.get("join_channel_id") or None,
                data.get("auto_role_id") or None,
                data.get("join_message_mode", "random"),
                int(bool(data.get("leave_enabled"))),
                data.get("leave_channel_id") or None,
                int(bool(data.get("rules_enabled"))),
                data.get("rules_channel_id") or None,
                data.get("rules_role_id") or None,
                data.get("rules_button_text", "✅ I Accept"),
            ))
            await db.commit()

    if request.method == "POST":
        run_async(save_config(request.form.to_dict()))
        log_action(guild_id, "Updated welcome settings", "config_welcome")
        return redirect(url_for("config_welcome") + "?saved=1")

    config = run_async(get_config())
    ctx    = get_current_user_context()
    return render("config/welcome.html",
                  config=config, saved=request.args.get("saved"), **ctx)


# ── Config: Boost ──────────────────────────────────────────────────────────────

@app.route("/config/boost", methods=["GET", "POST"])
@require_page("boost")
def config_boost():
    guild_id = get_session_guild_id()

    async def get_config():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM boost_config WHERE guild_id=?", (guild_id,))
            row = await cursor.fetchone()
            if row:
                return dict(zip([d[0] for d in cursor.description], row))
        return {}

    async def save_config(data: dict):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO boost_config
                    (guild_id, enabled, boost1_role_id, boost2_role_id,
                     boost2_channel_id, color_roles_enabled,
                     auto_remove_on_unboost)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    enabled                = excluded.enabled,
                    boost1_role_id         = excluded.boost1_role_id,
                    boost2_role_id         = excluded.boost2_role_id,
                    boost2_channel_id      = excluded.boost2_channel_id,
                    color_roles_enabled    = excluded.color_roles_enabled,
                    auto_remove_on_unboost = excluded.auto_remove_on_unboost
            """, (
                guild_id,
                int(bool(data.get("enabled", True))),
                data.get("boost1_role_id") or None,
                data.get("boost2_role_id") or None,
                data.get("boost2_channel_id") or None,
                int(bool(data.get("color_roles_enabled"))),
                int(bool(data.get("auto_remove_on_unboost", True))),
            ))
            await db.commit()

    if request.method == "POST":
        run_async(save_config(request.form.to_dict()))
        log_action(guild_id, "Updated boost config", "config_boost")
        return redirect(url_for("config_boost") + "?saved=1")

    config = run_async(get_config())
    ctx    = get_current_user_context()
    return render("config/boost.html",
                  config=config, saved=request.args.get("saved"), **ctx)


# ── Config: Announcements ──────────────────────────────────────────────────────

@app.route("/config/announcements")
@require_page("announcements")
def config_announcements():
    guild_id = get_session_guild_id()

    async def get_configs():
        async with aiosqlite.connect(DB_PATH) as db:
            yt = await (await db.execute(
                "SELECT * FROM youtube_config WHERE guild_id=?",
                (guild_id,))).fetchall()
            tw = await (await db.execute(
                "SELECT * FROM twitch_config WHERE guild_id=?",
                (guild_id,))).fetchall()
        return yt, tw

    yt, tw = run_async(get_configs())
    ctx    = get_current_user_context()
    return render("config/announcements.html", youtube=yt, twitch=tw, **ctx)


# ── Commands dashboard ─────────────────────────────────────────────────────────

COMMAND_CATEGORIES = {
    "Moderation": [
        "kick","ban","unban","timeout","untimeout",
        "warn","warnings","clearwarnings","purge",
        "lock","unlock","slowmode","modlogs",
    ],
    "Economy": [
        "balance","daily","work","give","richest",
        "addcoins","removecoins","shop","buy",
    ],
    "Leveling": ["rank","leaderboard","setxp","resetxp"],
    "Fun": ["hug","pat","slap","kiss","dance","coinflip","8ball"],
    "Utility": [
        "embed_create","embed_edit","sticky_set","sticky_remove",
        "trigger_add","trigger_remove","trigger_list",
    ],
    "Config": ["boost_setup","youtube_setup","youtube_remove","ticket_setup"],
    "Events": ["event_create","event_end","event_list"],
    "Tickets": [
        "ticket_close","ticket_claim","ticket_transfer",
        "reactionrole_create","reactionrole_add",
    ],
}


@app.route("/commands")
@require_page("commands")
def commands_dashboard():
    guild_id = get_session_guild_id()

    async def get_toggles():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT command_name, enabled, allowed_roles, allowed_channels,
                       cooldown_seconds, aliases, enabled_roles, disabled_roles,
                       enabled_channels, disabled_channels, delete_user_msg,
                       delete_bot_reply, delete_bot_after, custom_cooldown,
                       success_message, error_message, ephemeral, dm_response,
                       bypass_cooldown_roles, require_permission, owner_only,
                       cmd_emoji, category_color, hide_from_help
                FROM command_toggles WHERE guild_id=?
            """, (guild_id,))
            rows   = await cursor.fetchall()
            result = {}
            for r in rows:
                result[r[0]] = {
                    "enabled": r[1], "allowed_roles": r[2],
                    "allowed_channels": r[3], "cooldown": r[4],
                    "aliases": r[5], "enabled_roles": r[6],
                    "disabled_roles": r[7], "enabled_channels": r[8],
                    "disabled_channels": r[9], "delete_user_msg": r[10],
                    "delete_bot_reply": r[11], "delete_bot_after": r[12],
                    "custom_cooldown": r[13], "success_message": r[14],
                    "error_message": r[15], "ephemeral": r[16],
                    "dm_response": r[17], "bypass_cooldown_roles": r[18],
                    "require_permission": r[19], "owner_only": r[20],
                    "cmd_emoji": r[21], "category_color": r[22],
                    "hide_from_help": r[23],
                }
            return result

    toggles = run_async(get_toggles())
    ctx     = get_current_user_context()
    return render("manage/commands.html",
                  categories=COMMAND_CATEGORIES, toggles=toggles, **ctx)


@app.route("/config/commands", methods=["GET", "POST"])
@require_page("commands")
def config_commands():
    if request.method == "POST":
        guild_id = get_session_guild_id()
        command  = request.form.get("command")
        action   = request.form.get("action")
        if command and action:
            async def toggle_cmd():
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        INSERT INTO command_toggles (guild_id, command_name, enabled)
                        VALUES (?,?,?)
                        ON CONFLICT(guild_id, command_name)
                        DO UPDATE SET enabled=excluded.enabled,
                                      updated_at=CURRENT_TIMESTAMP
                    """, (guild_id, command, int(action == "enable")))
                    await db.commit()
            run_async(toggle_cmd())
        return redirect(url_for("config_commands"))
    return redirect(url_for("commands_dashboard"))


# ── Commands API ───────────────────────────────────────────────────────────────

@app.route("/api/commands/toggle", methods=["POST"])
@require_page("commands")
def api_command_toggle():
    guild_id = get_session_guild_id()
    data     = request.json
    command  = data.get("command")
    enabled  = data.get("enabled", True)

    async def toggle():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO command_toggles (guild_id, command_name, enabled)
                VALUES (?,?,?)
                ON CONFLICT(guild_id, command_name)
                DO UPDATE SET enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP
            """, (guild_id, command, int(bool(enabled))))
            await db.commit()

    run_async(toggle())
    log_action(guild_id,
               f"{'Enabled' if enabled else 'Disabled'} /{command}", "commands")
    return jsonify({"success": True})


@app.route("/api/commands/bulk-toggle", methods=["POST"])
@require_page("commands")
def api_commands_bulk_toggle():
    guild_id = get_session_guild_id()
    data     = request.json
    commands = data.get("commands", [])
    enabled  = data.get("enabled", True)
    category = data.get("category")

    if not commands:
        if category and category in COMMAND_CATEGORIES:
            commands = COMMAND_CATEGORIES[category]
        else:
            commands = [c for cmds in COMMAND_CATEGORIES.values() for c in cmds]

    async def bulk():
        async with aiosqlite.connect(DB_PATH) as db:
            for cmd in commands:
                await db.execute("""
                    INSERT INTO command_toggles (guild_id, command_name, enabled)
                    VALUES (?,?,?)
                    ON CONFLICT(guild_id, command_name)
                    DO UPDATE SET enabled=excluded.enabled,
                                  updated_at=CURRENT_TIMESTAMP
                """, (guild_id, cmd, int(bool(enabled))))
            await db.commit()

    run_async(bulk())
    log_action(guild_id,
               f"Bulk {'enabled' if enabled else 'disabled'} {len(commands)} commands",
               "commands")
    return jsonify({"success": True, "count": len(commands)})


@app.route("/api/commands/settings/<command>", methods=["GET"])
@require_page("commands")
def api_command_settings_get(command: str):
    guild_id = get_session_guild_id()

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT command_name, enabled, allowed_roles, allowed_channels,
                       cooldown_seconds, aliases, enabled_roles, disabled_roles,
                       enabled_channels, disabled_channels, delete_user_msg,
                       delete_bot_reply, delete_bot_after, custom_cooldown,
                       success_message, error_message, ephemeral, dm_response,
                       bypass_cooldown_roles, require_permission, owner_only,
                       cmd_emoji, category_color, hide_from_help
                FROM command_toggles WHERE guild_id=? AND command_name=?
            """, (guild_id, command))
            row = await cur.fetchone()
            if not row:
                return {"command_name": command, "enabled": 1}
            return dict(zip([d[0] for d in cur.description], row))

    return jsonify(run_async(get()))


@app.route("/api/commands/settings/<command>", methods=["POST"])
@require_page("commands")
def api_command_settings_save(command: str):
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO command_toggles
                    (guild_id, command_name, enabled, allowed_roles,
                     allowed_channels, cooldown_seconds, aliases,
                     enabled_roles, disabled_roles, enabled_channels,
                     disabled_channels, delete_user_msg, delete_bot_reply,
                     delete_bot_after, custom_cooldown, success_message,
                     error_message, ephemeral, dm_response,
                     bypass_cooldown_roles, require_permission, owner_only,
                     cmd_emoji, category_color, hide_from_help)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id, command_name) DO UPDATE SET
                    enabled=excluded.enabled,
                    allowed_roles=excluded.allowed_roles,
                    allowed_channels=excluded.allowed_channels,
                    cooldown_seconds=excluded.cooldown_seconds,
                    aliases=excluded.aliases,
                    enabled_roles=excluded.enabled_roles,
                    disabled_roles=excluded.disabled_roles,
                    enabled_channels=excluded.enabled_channels,
                    disabled_channels=excluded.disabled_channels,
                    delete_user_msg=excluded.delete_user_msg,
                    delete_bot_reply=excluded.delete_bot_reply,
                    delete_bot_after=excluded.delete_bot_after,
                    custom_cooldown=excluded.custom_cooldown,
                    success_message=excluded.success_message,
                    error_message=excluded.error_message,
                    ephemeral=excluded.ephemeral,
                    dm_response=excluded.dm_response,
                    bypass_cooldown_roles=excluded.bypass_cooldown_roles,
                    require_permission=excluded.require_permission,
                    owner_only=excluded.owner_only,
                    cmd_emoji=excluded.cmd_emoji,
                    category_color=excluded.category_color,
                    hide_from_help=excluded.hide_from_help,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                guild_id, command,
                int(bool(data.get("enabled", True))),
                json.dumps(data.get("allowed_roles", [])) if data.get("allowed_roles") else None,
                json.dumps(data.get("allowed_channels", [])) if data.get("allowed_channels") else None,
                data.get("cooldown_seconds"),
                json.dumps(data.get("aliases", [])) if data.get("aliases") else None,
                json.dumps(data.get("enabled_roles", [])) if data.get("enabled_roles") else None,
                json.dumps(data.get("disabled_roles", [])) if data.get("disabled_roles") else None,
                json.dumps(data.get("enabled_channels", [])) if data.get("enabled_channels") else None,
                json.dumps(data.get("disabled_channels", [])) if data.get("disabled_channels") else None,
                int(bool(data.get("delete_user_msg"))),
                int(bool(data.get("delete_bot_reply"))),
                int(data.get("delete_bot_after", 0)),
                data.get("custom_cooldown"),
                data.get("success_message"),
                data.get("error_message"),
                int(bool(data.get("ephemeral"))),
                int(bool(data.get("dm_response"))),
                json.dumps(data.get("bypass_cooldown_roles", [])) if data.get("bypass_cooldown_roles") else None,
                data.get("require_permission"),
                int(bool(data.get("owner_only"))),
                data.get("cmd_emoji"),
                data.get("category_color"),
                int(bool(data.get("hide_from_help"))),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Updated settings for /{command}", "commands")
    return jsonify({"success": True})


# ── Config: Access ─────────────────────────────────────────────────────────────

@app.route("/config/access", methods=["GET", "POST"])
@require_page("dashboard_access")
def config_access():
    guild_id = get_session_guild_id()

    async def get_users():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, user_id, permission_level,
                       added_by_name, enabled, added_at
                FROM dashboard_users WHERE guild_id=?
                ORDER BY added_at DESC
            """, (guild_id,))
            return await cursor.fetchall()

    async def add_user(user_id: int, level: str):
        user = current_user()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO dashboard_users
                    (guild_id, user_id, permission_level,
                     added_by, added_by_name)
                VALUES (?,?,?,?,?)
                ON CONFLICT DO NOTHING
            """, (
                guild_id, user_id, level,
                current_user_id(),
                user.get("username") if user else "Unknown",
            ))
            await db.commit()

    async def remove_user(entry_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM dashboard_users WHERE id=? AND guild_id=?",
                (entry_id, guild_id))
            await db.commit()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            uid   = int(request.form.get("user_id", 0))
            level = request.form.get("level", "moderator")
            run_async(add_user(uid, level))
            log_action(guild_id, f"Added {uid} as {level}",
                       "config_access", target_id=uid)
        elif action == "remove":
            entry_id = int(request.form.get("entry_id", 0))
            run_async(remove_user(entry_id))
            log_action(guild_id, f"Removed entry {entry_id}", "config_access")
        return redirect(url_for("config_access"))

    users = run_async(get_users())
    ctx   = get_current_user_context()
    return render("config/access.html", users=users, **ctx)


# ── Member edit API ────────────────────────────────────────────────────────────

@app.route("/api/edit-member", methods=["POST"])
@require_page("members_edit")
def api_edit_member():
    guild_id  = get_session_guild_id()
    data      = request.json
    user_id   = data.get("user_id")
    xp        = int(data.get("xp", 0))
    coins     = int(data.get("coins", 0))
    new_level = calculate_level_from_xp(xp)

    async def update():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (?,?,?,?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET xp=?, level=?
            """, (guild_id, user_id, xp, new_level, xp, new_level))
            await db.execute("""
                INSERT INTO economy (guild_id, user_id, balance)
                VALUES (?,?,?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET balance=?
            """, (guild_id, user_id, coins, coins))
            await db.commit()

    run_async(update())
    log_action(guild_id, f"Edited member {user_id}: xp={xp} coins={coins}",
               "members", target_id=int(user_id) if user_id else None)
    return jsonify({"success": True})


# ── Embed template API ─────────────────────────────────────────────────────────

@app.route("/api/save-embed-template", methods=["POST"])
@require_page("embedbuilder")
def api_save_embed_template():
    guild_id = get_session_guild_id()
    data     = request.json
    name     = data.get("name", "").lower().strip()
    embed    = data.get("embed", {})
    if not name:
        return jsonify({"success": False, "error": "Name required"})

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO embed_templates (guild_id, name, data)
                VALUES (?,?,?)
            """, (guild_id, name, json.dumps(embed)))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Saved embed template '{name}'", "embedbuilder")
    return jsonify({"success": True})


@app.route("/api/embed-templates")
@require_page("embedbuilder")
def api_embed_templates():
    guild_id = get_session_guild_id()

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT name FROM embed_templates WHERE guild_id=?", (guild_id,))
            return [r[0] for r in await cursor.fetchall()]

    return jsonify({"templates": run_async(get())})


@app.route("/api/embed-template/<n>", methods=["GET", "DELETE"])
@require_page("embedbuilder")
def api_embed_template(n: str):
    guild_id = get_session_guild_id()
    if request.method == "DELETE":
        async def delete():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM embed_templates WHERE guild_id=? AND name=?",
                    (guild_id, n))
                await db.commit()
        run_async(delete())
        return jsonify({"success": True})

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT data FROM embed_templates WHERE guild_id=? AND name=?",
                (guild_id, n))
            row = await cursor.fetchone()
            return json.loads(row[0]) if row else None

    return jsonify({"template": run_async(get())})


# ── Trigger API ────────────────────────────────────────────────────────────────

@app.route("/api/save-trigger", methods=["POST"])
@require_page("triggers")
def api_save_trigger():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO triggers
                    (guild_id, trigger_words, response_text,
                     response_embed, response_type, match_type,
                     fuzzy_match, case_sensitive, response_chance,
                     allowed_channels, enabled)
                VALUES (?,?,?,?,?,?,?,?,?,?,1)
            """, (
                guild_id,
                data.get("trigger_words"),
                data.get("response_text"),
                json.dumps(data.get("response_embed")) if data.get("response_embed") else None,
                data.get("response_type", "text"),
                data.get("match_type", "contains"),
                int(data.get("fuzzy_match", 0)),
                int(data.get("case_sensitive", 0)),
                int(data.get("response_chance", 100)),
                json.dumps(data.get("allowed_channels", [])),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Added trigger: {data.get('trigger_words')}", "triggers")
    return jsonify({"success": True})


@app.route("/api/delete-trigger/<int:trigger_id>", methods=["DELETE"])
@require_page("triggers")
def api_delete_trigger(trigger_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM triggers WHERE id=? AND guild_id=?",
                (trigger_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


# ── Custom command API ─────────────────────────────────────────────────────────

@app.route("/api/save-custom-command", methods=["POST"])
@require_page("customcommands")
def api_save_custom_command():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO custom_commands
                    (guild_id, trigger, allowed_roles, actions,
                     embed_title, embed_description, embed_color,
                     log_channel_id, same_channel, dm_member,
                     dm_message, requires_mention, requires_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                guild_id,
                data.get("trigger"),
                json.dumps(data.get("allowed_roles", [])),
                json.dumps(data.get("actions", [])),
                data.get("embed_title"),
                data.get("embed_description"),
                data.get("embed_color", "#ED4245"),
                data.get("log_channel_id"),
                int(bool(data.get("same_channel"))),
                int(bool(data.get("dm_member"))),
                data.get("dm_message"),
                int(bool(data.get("requires_mention", True))),
                int(bool(data.get("requires_reason", True))),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Added custom command: !{data.get('trigger')}",
               "customcommands")
    return jsonify({"success": True})


@app.route("/api/delete-custom-command/<int:cmd_id>", methods=["DELETE"])
@require_page("customcommands")
def api_delete_custom_command(cmd_id: int):
    guild_id = get_session_guild_id()

    async def delete():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM custom_commands WHERE id=? AND guild_id=?",
                (cmd_id, guild_id))
            await db.commit()

    run_async(delete())
    return jsonify({"success": True})


# ── Reaction role API ──────────────────────────────────────────────────────────

@app.route("/api/save-rr-panel", methods=["POST"])
@require_page("reactionroles")
def api_save_rr_panel():
    guild_id = get_session_guild_id()
    data     = request.json

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO rr_panels
                    (guild_id, title, description, color, channel_id, buttons,
                     exclusive, max_roles, require_confirmation, required_role)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                guild_id,
                data.get("title"),
                data.get("desc"),
                data.get("color"),
                data.get("channel"),
                json.dumps(data.get("buttons", [])),
                int(data.get("exclusive", 0)),
                int(data.get("max_roles", 0)),
                int(bool(data.get("require_confirmation"))),
                data.get("required_role", ""),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Saved RR panel: {data.get('title')}", "reactionroles")
    return jsonify({"success": True})


@app.route("/api/rr-panels")
@require_page("reactionroles")
def api_rr_panels():
    guild_id = get_session_guild_id()

    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, title, buttons FROM rr_panels WHERE guild_id=? ORDER BY id DESC",
                (guild_id,))
            rows = await cursor.fetchall()
            return [{"id": r[0], "title": r[1],
                     "buttons": len(json.loads(r[2])) if r[2] else 0}
                    for r in rows]

    return jsonify({"panels": run_async(get())})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
