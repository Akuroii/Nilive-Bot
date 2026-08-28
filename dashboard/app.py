import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import math
import time
from functools import wraps
import aiosqlite
from dotenv import load_dotenv
from flask import (
    Flask, redirect, url_for, session,
    request, render_template, jsonify, abort,
)
from database import DB_PATH, init_db, NERO_ENVIRONMENT
from dashboard.utils.async_utils import run_async
from dashboard.auth import (
    login_required, create_session, clear_session,
    get_discord_oauth_url, exchange_code, fetch_discord_user,
    fetch_discord_guilds, current_user, current_user_id,
    verify_oauth_state, consume_oauth_remember,
)
from dashboard.permissions import (
    require_page, get_current_user_context, log_action,
    get_session_guild_id, set_session_guild,
    require_bot_owner, is_trusted_super_admin,
    LEVEL_RANK, LEVEL_OWNER,
)
from dashboard.api import api_bp
from utils.xp_calculator import calculate_level_from_xp
from utils.formatters import format_relative, format_timestamp

# staging-db delta: dashboard/app.py never called load_dotenv() itself —
# it worked anyway in production because Railway injects real env vars
# directly (no .env file involved), but any local/dev run relying on a
# .env file for DATABASE_PATH / NERO_ENVIRONMENT / SECRET_KEY etc. would
# silently not see them, since database.py (imported two lines up) reads
# those at MODULE IMPORT time.
load_dotenv()

app = Flask(__name__,
            template_folder="templates",
            static_folder="static")

_secret_key = os.getenv("SECRET_KEY", "").strip()
if not _secret_key:
    print("=" * 60)
    print("FATAL: SECRET_KEY is missing or empty.")
    print("  -> Dashboard sessions (including the CSRF token and the")
    print("     logged-in user's identity) are signed with this key.")
    print("     Running with no key, or a hardcoded fallback checked")
    print("     into source control, lets an attacker who knows that")
    print("     value forge a valid session for ANY user, including")
    print("     the owner, with no login required.")
    print("  -> Set SECRET_KEY in Railway > your service > Variables")
    print("     to a long random value, e.g.:")
    print("       python -c \"import secrets; print(secrets.token_hex(32))\"")
    print("=" * 60)
    sys.exit(1)
if len(_secret_key) < 32:
    print("=" * 60)
    print("WARNING: SECRET_KEY is set but shorter than 32 characters.")
    print("  -> A short/guessable key is still forgeable. Generate a")
    print("     longer one with:")
    print("       python -c \"import secrets; print(secrets.token_hex(32))\"")
    print("  -> Continuing anyway, but treat this as urgent to rotate.")
    print("=" * 60)

app.secret_key = _secret_key
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7

# SECURITY FIX: the session cookie (which carries the signed user
# identity + CSRF token) previously had no explicit Secure/HttpOnly/
# SameSite flags, so Flask's defaults applied — HttpOnly is on by
# default, but Secure is NOT, meaning the cookie could legally be sent
# over a plain HTTP connection if one ever existed in the request path
# (a misconfigured proxy, HTTP-only health checks hitting the same
# host, etc). SameSite=Lax blocks the cookie being sent on cross-site
# requests except top-level GET navigations (the normal case for
# following a link here), which is good baseline CSRF-adjacent
# hardening on top of the existing X-CSRF-Token mechanism, not a
# replacement for it.
#
# DASHBOARD_FORCE_HTTP is an explicit, opt-in escape hatch for local
# dev over plain http://localhost — Secure cookies are simply never
# sent by browsers over HTTP, so leaving Secure on in that setup would
# make login appear to silently fail with no obvious cause. Unset (the
# default) is correct for the real Oracle Cloud deployment, which
# should terminate TLS in front of the app.
_force_http = os.getenv("DASHBOARD_FORCE_HTTP", "").strip().lower() in ("1", "true", "yes")
app.config["SESSION_COOKIE_SECURE"]   = not _force_http
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

app.register_blueprint(api_bp)

print("Initializing database (dashboard process)...")
run_async(init_db())
print("Database ready (dashboard process).")


_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.before_request
def _enforce_csrf_app_level():
    if not request.path.startswith("/api/"):
        return
    if request.method in _CSRF_SAFE_METHODS:
        return
    session_token = session.get("csrf_token")
    header_token = request.headers.get("X-CSRF-Token", "")
    if not session_token or not header_token or header_token != session_token:
        return jsonify({
            "success": False,
            "error": "CSRF validation failed. Refresh the page and try again.",
        }), 403


def render(template, **ctx):
    return render_template(template, **ctx)


# SECURITY FIX: baseline response security headers — none of these
# were set anywhere before. Frame/MIME/referrer hardening is
# unconditional; HSTS only makes sense once the connection is actually
# HTTPS (harmless but pointless over plain HTTP dev), so it's skipped
# when DASHBOARD_FORCE_HTTP opts out of Secure cookies too.
#
# img-src is deliberately looser than a strict "self + Discord's CDN
# only" policy would be. Several existing, intentional features let an
# admin paste an arbitrary image URL — Embed Builder's thumbnail/image
# fields, Welcome's embed images, Bot Profile's avatar/banner preview,
# ticket category embeds, Creator Hub's YouTube/Twitch thumbnails —
# none of which are limited to Discord's CDN. A stricter img-src would
# silently break every one of those previews with no visible error
# beyond "the image just doesn't show up". Allowing any https: image
# source keeps that intact while still blocking plain-http image loads.
@app.after_request
def _set_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not _force_http:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    return response


# SECURITY FIX: /login, /discord_login, and /callback are the only
# routes reachable with zero authentication at all — everything else
# sits behind @login_required or @require_page. Nothing previously
# rate-limited repeated hits against them, which matters most for
# /callback (repeatedly replaying/guessing an authorization code) and
# /discord_login (hammering the OAuth redirect). Keyed by remote IP; a
# small in-memory sliding window, not a distributed store — sufficient
# for this project's actual scale (a handful of trusted admins), not
# meant to survive a multi-instance deployment. Same opportunistic-
# prune shape already used for the cooldown dicts in cogs/economy.py,
# cogs/triggers.py, and main.py.
_rate_limit_buckets: dict[str, list] = {}


def rate_limit(max_requests: int, window_seconds: int):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            key = f"{request.remote_addr}:{f.__name__}"
            now = time.time()
            bucket = _rate_limit_buckets.get(key, [])
            bucket = [t for t in bucket if now - t < window_seconds]
            if len(bucket) >= max_requests:
                abort(429)
            bucket.append(now)
            _rate_limit_buckets[key] = bucket

            if len(_rate_limit_buckets) > 2000:
                cutoff = now - window_seconds
                for k in [k for k, v in _rate_limit_buckets.items()
                          if not v or v[-1] < cutoff]:
                    del _rate_limit_buckets[k]

            return f(*args, **kwargs)
        return wrapped
    return decorator


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"success": False, "error": "Too many requests — try again shortly."}), 429


# staging-db feature: makes nero_environment/is_staging available to
# EVERY template render (context processors apply globally, not just
# to calls through this file's render() helper) — used by the staging
# banner in base.html, the staging badge on login.html, and the
# Environment row on health.html.
@app.context_processor
def inject_environment():
    return {
        "nero_environment": NERO_ENVIRONMENT,
        "is_staging": NERO_ENVIRONMENT == "staging",
    }


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
@rate_limit(20, 60)
def login():
    if session.get("user"):
        return redirect(url_for("server_select"))
    return render_template("login.html")


@app.route("/discord_login")
@rate_limit(10, 60)
def discord_login():
    # SECURITY FIX: the OAuth authorize URL previously carried no
    # `state` parameter at all — textbook OAuth CSRF: nothing stopped
    # an attacker from tricking a victim's browser into completing an
    # authorization flow the attacker initiated (e.g. to bind the
    # victim's session to an attacker-controlled Discord account, or
    # replay a captured callback URL). get_discord_oauth_url() now
    # mints a random state, stashes it in the session, and /callback
    # below verifies the round-tripped value matches before doing
    # anything else.
    #
    # BUGFIX, same change: `remember` was previously read from
    # request.args at /callback, but Discord's redirect_uri is a
    # fixed, pre-registered value — nothing about the ORIGINAL
    # /discord_login request (including ?remember=1) survives the
    # round trip to Discord and back. "Remember me" was silently a
    # no-op regardless of the checkbox. It's now stashed in the
    # session here (alongside state) and read back by
    # consume_oauth_remember() in /callback, which actually survives
    # the redirect.
    remember = request.args.get("remember") == "1"
    return redirect(get_discord_oauth_url(remember=remember))


@app.route("/callback")
@rate_limit(15, 60)
def callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    if not code:
        return redirect(url_for("login"))
    if not verify_oauth_state(state):
        # Expired session, replayed/forged callback, or a genuine CSRF
        # attempt — indistinguishable from here, and all three get the
        # same safe response: back to login, no session created.
        return redirect(url_for("login"))
    tokens = exchange_code(code)
    if not tokens or not tokens.get("access_token"):
        return redirect(url_for("login"))
    user = fetch_discord_user(tokens["access_token"])
    if not user:
        return redirect(url_for("login"))
    remember = consume_oauth_remember()
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
        user_id = current_user_id()

        # Developer bypass — every guild the BOT is currently in, not
        # just guilds the logged-in developer personally belongs to
        # on Discord. fetch_discord_guilds() above is scoped to the
        # LOGGED-IN USER's own OAuth membership, which would
        # otherwise hide any server the developer isn't personally a
        # member of even though their bypass access covers it.
        # Checked before the "no guilds" early return below, since an
        # empty personal guild list shouldn't short-circuit this.
        if is_trusted_super_admin(user_id):
            from dashboard.auth import fetch_bot_guilds_full
            bot_guilds = fetch_bot_guilds_full()
            return [{
                "id":    int(g["id"]),
                "name":  g.get("name", "Unknown Server"),
                "icon":  g.get("icon"),
                "level": LEVEL_OWNER,
            } for g in bot_guilds]

        if not guilds:
            return []
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
    level = row[0] if row else None

    # Developer bypass — never writes a dashboard_users row, so the
    # developer never appears in Current Access. bot_is_in_guild is
    # still checked so a guessed/typo'd guild_id the bot isn't
    # actually in still 403s instead of silently "succeeding".
    if not level and is_trusted_super_admin(user_id):
        from dashboard.auth import bot_is_in_guild
        if bot_is_in_guild(guild_id):
            level = LEVEL_OWNER

    if not level:
        abort(403)

    set_session_guild(guild_id)
    session["user_level"] = level

    access_token = session.get("access_token", "")
    if access_token:
        guilds = fetch_discord_guilds(access_token)
        gdata  = next((g for g in guilds if int(g["id"]) == guild_id), None)
        session["guild_name"] = gdata["name"] if gdata else ""

    return redirect(url_for("index"))


# ── Phase 0 Extension — Server Permission Gating (Sapphire-style) ──────────

@app.route("/api/user/servers")
@login_required
def api_user_servers():
    from dashboard.auth import (
        guild_permissions_include_admin, fetch_bot_guilds_full,
    )

    access_token = session.get("access_token", "")
    if not access_token:
        return jsonify({"servers": []})

    guilds     = fetch_discord_guilds(access_token)
    # BUGFIX: this used to source name/icon purely from the user's own
    # OAuth guild list (fetch_discord_guilds) and only use the bot's
    # guild data for the is_bot_member flag. Confirmed by screenshot
    # that this OAuth list can carry icon: null for a guild that does
    # have one set (name and bot-installed status came through fine,
    # icon specifically didn't) — root cause on Discord's side wasn't
    # pinned down, so rather than depend on that diagnosis, guilds the
    # bot is actually in now use the bot's own (bot-token) view of
    # name/icon instead, which is already proven reliable — same data
    # /server-select's developer-bypass view already uses. Guilds the
    # bot ISN'T in still fall back to the OAuth data, same as before.
    bot_guilds = {int(g["id"]): g for g in fetch_bot_guilds_full()}

    servers = []
    for g in guilds:
        if not guild_permissions_include_admin(g.get("permissions")):
            continue
        gid      = int(g["id"])
        bot_data = bot_guilds.get(gid)
        servers.append({
            "id":            gid,
            "name":          (bot_data or g).get("name", "Unknown Server"),
            "icon":          (bot_data or g).get("icon"),
            "is_bot_member": bot_data is not None,
        })

    servers.sort(key=lambda s: (not s["is_bot_member"], s["name"].lower()))
    return jsonify({"servers": servers})


@app.route("/api/invite-bot/<int:guild_id>")
@login_required
def api_invite_bot(guild_id: int):
    from dashboard.auth import get_bot_invite_url
    return jsonify({"url": get_bot_invite_url(guild_id)})


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
                       COALESCE(e.balance, 0) AS coins,
                       COALESCE(e.diamonds, 0) AS diamonds
                FROM levels l
                LEFT JOIN economy e
                  ON l.user_id = e.user_id AND l.guild_id = e.guild_id
                WHERE l.guild_id = ?
                ORDER BY l.xp DESC LIMIT 100
            """, (guild_id,))
            rows = await cursor.fetchall()
        return [{"user_id": r[0], "xp": r[1],
                 "level": r[2], "coins": r[3], "diamonds": r[4]} for r in rows]

    member_list = run_async(get_members())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = [m["user_id"] for m in member_list]
        if not ids:
            return {}
        return await resolve_users(guild_id, ids)

    user_map = run_async(resolve())
    ctx      = get_current_user_context()
    return render("general/members.html", members=member_list,
                  user_map=user_map, **ctx)


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
                "SELECT balance, diamonds FROM economy WHERE guild_id=? AND user_id=?",
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
            "diamonds": econ_row[1]  if econ_row  else 0,
            "warnings": warnings,
            "mod_logs": mod_logs,
            "purchases": purchases,
        }

    profile = run_async(get_profile())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        return await resolve_users(guild_id, [user_id])

    user_map = run_async(resolve())
    ctx      = get_current_user_context()
    return render("general/member_profile.html",
                  profile=profile, member_id=user_id, user_map=user_map, **ctx)


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


# ── Reports (P1 #16) ────────────────────────────────────────────────────────

@app.route("/reports")
@require_page("reports")
def reports():
    guild_id = get_session_guild_id()
    status   = request.args.get("status", "open")

    async def get_data():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, reporter_name, reported_user_name, reason,
                       message_jump_url, status, created_at, resolved_by_name
                FROM reports
                WHERE guild_id = ? AND status = ?
                ORDER BY created_at DESC LIMIT 100
            """, (guild_id, status))
            rows = await cursor.fetchall()

            counts_cur = await db.execute("""
                SELECT status, COUNT(*) FROM reports
                WHERE guild_id = ? GROUP BY status
            """, (guild_id,))
            counts_rows = await counts_cur.fetchall()

            config_cur = await db.execute(
                "SELECT enabled, report_channel_id, staff_role_id "
                "FROM report_config WHERE guild_id = ?", (guild_id,))
            config_row = await config_cur.fetchone()

        counts = {row[0]: row[1] for row in counts_rows}
        return rows, counts, config_row

    reports_list, counts, config_row = run_async(get_data())
    config = {
        "enabled": bool(config_row[0]) if config_row else False,
        "report_channel_id": config_row[1] if config_row else None,
        "staff_role_id": config_row[2] if config_row else None,
    }
    ctx = get_current_user_context()
    return render(
        "general/reports.html",
        reports=reports_list, status=status, counts=counts,
        config=config, **ctx)


# ── Health (P1 #17) ─────────────────────────────────────────────────────────

@app.route("/health")
@require_page("health")
def health():
    async def get_status():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT * FROM bot_status WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return dict(zip([d[0] for d in cursor.description], row))
        return None

    row = run_async(get_status())

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def _parse(ts):
        if not ts:
            return None
        try:
            return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return None

    last_hb  = _parse(row.get("last_heartbeat")) if row else None
    started  = _parse(row.get("started_at")) if row else None
    seconds_since_hb = (now - last_hb).total_seconds() if last_hb else None

    is_online = seconds_since_hb is not None and seconds_since_hb < 90

    uptime_str = "Unknown"
    if started:
        delta = now - started
        days, rem = divmod(int(delta.total_seconds()), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = []
        if days: parts.append(f"{days}d")
        if hours: parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        uptime_str = " ".join(parts)

    loaded_cogs = json.loads(row.get("loaded_cogs") or "[]") if row else []
    failed_cogs = json.loads(row.get("failed_cogs") or "[]") if row else []

    db_size_bytes = None
    db_exists     = os.path.exists(DB_PATH)
    if db_exists:
        try:
            db_size_bytes = os.path.getsize(DB_PATH)
        except OSError:
            db_size_bytes = None

    ctx = get_current_user_context()
    return render(
        "general/health.html",
        row=row,
        is_online=is_online,
        seconds_since_hb=seconds_since_hb,
        uptime_str=uptime_str,
        loaded_cogs=loaded_cogs,
        failed_cogs=failed_cogs,
        db_path=DB_PATH,
        db_exists=db_exists,
        db_size_bytes=db_size_bytes,
        **ctx)


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

            try:
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
            except Exception:
                active_punishments = []

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
            "tickets":      ticket_list,
            "avg_rating":   avg_rating,
            "rating_count": rating_count,
        }

    data = run_async(get_data())
    tickets = data.get("tickets", [])
    data["ticket_open"]   = sum(1 for t in tickets if t[3] == "open")
    data["ticket_closed"] = sum(1 for t in tickets if t[3] == "closed")
    data["ticket_total"]  = len(tickets)

    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = [t[2] for t in tickets]
        if not ids:
            return {}
        return await resolve_users(guild_id, ids)

    data["user_map"] = run_async(resolve())
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

    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = [s[0] for s in scores]
        if not ids:
            return {}
        return await resolve_users(guild_id, ids)

    user_map = run_async(resolve())
    ctx      = get_current_user_context()
    return render("systems/mvp.html", scores=scores, history=history,
                  user_map=user_map, **ctx)


# ── Leveling ───────────────────────────────────────────────────────────────────

@app.route("/leveling")
@require_page("leveling")
def leveling():
    guild_id = get_session_guild_id()

    async def get_data():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level, prestige FROM levels
                WHERE guild_id=? ORDER BY prestige DESC, xp DESC LIMIT 50
            """, (guild_id,))
            levels = await cursor.fetchall()
            rewards_cursor = await db.execute("""
                SELECT id, level, role_id FROM leveling_rewards
                WHERE guild_id=? ORDER BY level ASC
            """, (guild_id,))
            rewards = await rewards_cursor.fetchall()
        return levels, rewards

    levels, rewards = run_async(get_data())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = [l[0] for l in levels]
        if not ids:
            return {}
        return await resolve_users(guild_id, ids)

    user_map = run_async(resolve())
    ctx      = get_current_user_context()
    return render("systems/leveling.html", levels=levels, rewards=rewards,
                  user_map=user_map, **ctx)


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
            balances = await cursor.fetchall()

            dcursor = await db.execute("""
                SELECT user_id, diamonds FROM economy
                WHERE guild_id=? AND diamonds > 0
                ORDER BY diamonds DESC LIMIT 50
            """, (guild_id,))
            diamonds = await dcursor.fetchall()

            rcursor = await db.execute(
                "SELECT diamond_exchange_rate FROM guild_settings WHERE guild_id=?",
                (guild_id,))
            rrow = await rcursor.fetchone()
        return balances, diamonds, (rrow[0] if rrow and rrow[0] else 500)

    balances, diamonds, exchange_rate = run_async(get_data())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = {r[0] for r in balances} | {r[0] for r in diamonds}
        if not ids:
            return {}
        return await resolve_users(guild_id, list(ids))

    user_map = run_async(resolve())
    ctx = get_current_user_context()
    return render("systems/economy.html",
                  balances=balances, diamonds=diamonds,
                  exchange_rate=exchange_rate, user_map=user_map, **ctx)


# ── Shop ───────────────────────────────────────────────────────────────────────

@app.route("/shop")
@require_page("shop")
def shop():
    guild_id = get_session_guild_id()

    async def get_items():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, name, description, price, type,
                       role_id, duration_hours, featured, enabled,
                       price_diamonds
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


# ── Minigames / Event Stack Builder (dark-fixes pass #13) ───────────────────

@app.route("/minigames")
@require_page("minigames")
def minigames_page():
    guild_id = get_session_guild_id()

    async def get_data():
        from cogs.minigames import ensure_tables, get_config, get_tiers
        await ensure_tables()
        config = await get_config(guild_id)
        tiers  = await get_tiers(guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT event_date, tier, winner_id, winner_display_name,
                       forced, fired_at
                FROM minigames_log
                WHERE guild_id = ?
                ORDER BY fired_at DESC LIMIT 25
            """, (guild_id,))
            log = await cursor.fetchall()
        return config, tiers, log

    config, tiers, log = run_async(get_data())
    ctx = get_current_user_context()
    return render("systems/minigames.html",
                  config=config, tiers=tiers, log=log, **ctx)


# ── Missions ─────────────────────────────────────────────────────────────────

@app.route("/missions")
@require_page("missions")
def missions_page():
    guild_id = get_session_guild_id()
    ctx = get_current_user_context()
    return render("systems/missions.html", **ctx)


# ── Ledger (Phase 3 E3 CLOSEOUT — read-only) ────────────────────────────────

@app.route("/ledger")
@require_page("ledger")
def ledger_page():
    guild_id = get_session_guild_id()

    async def get_recent():
        from utils.ledger import get_guild_ledger
        return await get_guild_ledger(guild_id, limit=100)

    entries = run_async(get_recent())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = [e["user_id"] for e in entries]
        if not ids:
            return {}
        return await resolve_users(guild_id, ids)

    user_map = run_async(resolve())
    ctx      = get_current_user_context()
    return render("systems/ledger.html", entries=entries,
                  user_map=user_map, **ctx)


# ── Inventory (Phase 3 E4 CLOSEOUT — read-only) ─────────────────────────────

@app.route("/inventory")
@require_page("inventory_view")
def inventory_page():
    guild_id = get_session_guild_id()

    async def get_summary():
        from utils.inventory import get_guild_inventory_summary
        return await get_guild_inventory_summary(guild_id, limit=200)

    items = run_async(get_summary())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = [it["user_id"] for it in items]
        if not ids:
            return {}
        return await resolve_users(guild_id, ids)

    user_map = run_async(resolve())
    ctx      = get_current_user_context()
    return render("systems/inventory.html",
                  items=items, target_user_id=None, user_map=user_map, **ctx)


@app.route("/inventory/<int:user_id>")
@require_page("inventory_view")
def inventory_user_page(user_id: int):
    guild_id = get_session_guild_id()

    async def get_user_items():
        from utils.inventory import get_inventory
        return await get_inventory(guild_id, user_id, include_empty=False)

    items = run_async(get_user_items())

    async def resolve():
        from utils.discord_user_cache import resolve_users
        return await resolve_users(guild_id, [user_id])

    user_map = run_async(resolve())
    ctx      = get_current_user_context()
    return render("systems/inventory.html",
                  items=items, target_user_id=user_id, user_map=user_map, **ctx)


# ── Trade (read-only history) ───────────────────────────────────────────────

@app.route("/trade")
@require_page("trade")
def trade_page():
    guild_id = get_session_guild_id()
    ctx = get_current_user_context()
    return render("systems/trade.html", **ctx)


# ── Backups (bot-wide — every guild's data lives in one DB file) ───────────

@app.route("/backups")
@require_page("backups")
@require_bot_owner
def backups():
    async def get_backups():
        async with aiosqlite.connect(DB_PATH) as db:
            # id DESC tie-break: created_at is 1-second resolution, and two
            # backups CAN land in the same second (the manual trigger makes
            # this realistic in a way the old daily-only cron never was).
            # Without the tie-break, SQLite's order among tied rows is scan
            # order, not insertion order, so "most recent first" would
            # silently be wrong sometimes.
            cursor = await db.execute("""
                SELECT id, filename, size_bytes, created_at
                FROM backup_log
                ORDER BY created_at DESC, id DESC
            """)
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, r)) for r in rows]

    from cogs.backup import BACKUP_DIR, KEEP_BACKUPS

    def fmt_size(n):
        n = n or 0
        if n >= 1024 * 1024:
            return f"{n / 1024 / 1024:.2f} MB"
        if n >= 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n} B"

    raw_rows = run_async(get_backups())
    rows = []
    for r in raw_rows:
        rows.append({
            **r,
            "size_display":     fmt_size(r["size_bytes"]),
            "created_display":  format_timestamp(r["created_at"]),
            "created_relative": format_relative(r["created_at"]),
            "exists_on_disk":   os.path.exists(os.path.join(BACKUP_DIR, r["filename"])),
        })

    total_display = fmt_size(sum((r["size_bytes"] or 0) for r in raw_rows))

    ctx = get_current_user_context()
    return render(
        "general/backups.html",
        backups=rows,
        total_display=total_display,
        keep_backups=KEEP_BACKUPS,
        backup_dir=BACKUP_DIR,
        **ctx)


# ── Server Tags — Tag-Loyalty Missions + Cross-Server Join Reward ──────────

@app.route("/tag-missions")
@require_page("tagmissions")
def tagmissions_page():
    ctx = get_current_user_context()
    return render("systems/tagmissions.html", **ctx)


@app.route("/tag-partners")
@require_page("tagpartners")
def tagpartners_page():
    ctx = get_current_user_context()
    return render("systems/tagpartners.html", **ctx)


# ── Creator Hub (YouTube / Twitch) ──────────────────────────────────────────
#
# Replaces the old read-only "Announcements" page. Full add/delete/toggle
# CRUD lives client-side via /api/creator/* (dashboard/api/creator.py);
# this route just renders the shell, same pattern as /missions and
# /minigames (both of which also load their data via fetch()).

@app.route("/creator")
@require_page("creator")
def creator_page():
    ctx = get_current_user_context()
    return render("config/creator.html", **ctx)


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


# ── Config: Bot Profile (per-server nickname + branding icon) ──────────────────
#
# REGRESSION FIX (creator-notify pass): this route was silently dropped
# from app.py somewhere between the Bot Profile feature landing and the
# Creator Hub delta being built — the file that added /creator and the
# announcements->creator redirect was apparently edited from a copy of
# app.py that predated Bot Profile. dashboard/api/botprofile.py (the
# actual route handlers) was never touched or lost, only this page's
# entry point — restored verbatim from before the Creator Hub delta.

@app.route("/config/botprofile")
@require_page("botprofile")
def config_botprofile():
    ctx = get_current_user_context()
    return render("config/botprofile.html", **ctx)


# ── Config: Announcements (legacy — redirects to /creator) ─────────────────
#
# CREATOR pass: this used to render a read-only view of youtube_config +
# twitch_config. It's fully superseded by /creator (full CRUD), so the
# route stays only so any bookmarked/linked /config/announcements URL
# still lands somewhere useful instead of 404ing.

@app.route("/config/announcements")
@require_page("creator")
def config_announcements():
    return redirect(url_for("creator_page"))


# ── Commands dashboard ─────────────────────────────────────────────────────────

COMMAND_CATEGORIES = {
    "Moderation": [
        "kick", "ban", "unban", "timeout", "untimeout",
        "warn", "warnings", "clearwarnings", "purge",
        "lock", "unlock", "slowmode", "modlogs",
        "massban", "lockdown", "unlockdown",
    ],
    "Economy": [
        "balance", "daily", "give", "convert", "richest",
        "addcoins", "removecoins", "adddiamonds", "removediamonds",
    ],
    "Leveling": [
        "rank", "leaderboard", "setxp", "resetxp",
        "resetleaderboard", "prestige",
    ],
    "Shop & Inventory": [
        "shop", "inventory",
    ],
    "Tickets": [
        "ticket_setup", "ticket_add", "ticket_remove", "ticket_close",
    ],
    "Reaction Roles": [
        "reactionrole_create", "reactionrole_add",
        "reactionrole_remove", "reactionrole_list",
    ],
    "Embed Builder": [
        "embed_create", "embed_field", "embed_edit",
        "embed_send", "embed_list", "embed_delete_template",
    ],
    "Triggers & Sticky": [
        "trigger_add", "trigger_remove", "trigger_list", "trigger_toggle",
        "sticky_set", "sticky_remove", "sticky_list",
    ],
    "Minigames": [
        "minigames_setup", "minigames_tier_add", "minigames_tier_list",
        "minigames_tier_remove", "minigames_force", "minigames_stats",
    ],
    "Missions": [
        "missions", "mission_create", "mission_list", "mission_remove",
    ],
    "Events & MVP": [
        "event_create", "event_list",
        "mvp_scores", "mvp_setup", "mvp_force",
    ],
    "Creator & Alerts": [
        "youtube_setup", "youtube_remove", "youtube_list",
        "twitch_setup", "twitch_remove", "twitch_list",
    ],
    "Server Config": [
        "boost_setup", "boosters", "boostcolor_add", "boostcolor_remove",
        "boostcolor_list", "boostcolor", "welcome_setup", "welcome_test",
        "botprofile_view", "backup_now", "backup_list",
    ],
    "Utility & Trade": [
        "trade", "trade_history", "report_setup", "report_list",
        "schedule_message", "schedule_list", "schedule_cancel",
    ],
}

COMMAND_METADATA = {
    "backup_now": {"desc": "Trigger an immediate DB backup (owner only)", "params": []},
    "backup_list": {"desc": "List recent DB backups (owner only)", "params": []},
    "boost_setup": {"desc": "Configure boost roles and announcements", "params": ["boost1_role", "boost2_role", "announce_channel"]},
    "boosters": {"desc": "List current server boosters", "params": []},
    "boostcolor_add": {"desc": "Add a self-pickable color role for boosters", "params": ["role", "requires_boost_level"]},
    "boostcolor_remove": {"desc": "Remove a color role from the boost picker", "params": ["role"]},
    "boostcolor_list": {"desc": "List configured boost color role options", "params": []},
    "boostcolor": {"desc": "Pick your boost color role", "params": ["color"]},
    "botprofile_view": {"desc": "View this server's configured bot profile", "params": []},
    "balance": {"desc": "Check your coin and diamond balance", "params": ["member"]},
    "daily": {"desc": "Claim your daily coins", "params": []},
    "give": {"desc": "Give coins to another member", "params": ["member", "amount"]},
    "convert": {"desc": "Convert coins into diamonds", "params": ["coins"]},
    "richest": {"desc": "View the richest members", "params": []},
    "addcoins": {"desc": "Add coins to a member (admin)", "params": ["member", "amount"]},
    "removecoins": {"desc": "Remove coins from a member (admin)", "params": ["member", "amount"]},
    "adddiamonds": {"desc": "Add diamonds to a member (admin)", "params": ["member", "amount"]},
    "removediamonds": {"desc": "Remove diamonds from a member (admin)", "params": ["member", "amount"]},
    "embed_create": {"desc": "Create and send a custom embed", "params": ["channel", "title", "description", "color", "footer", "image", "thumbnail", "author", "save_as"]},
    "embed_field": {"desc": "Add a field to an existing embed", "params": ["message_id", "field_name", "field_value", "inline"]},
    "embed_edit": {"desc": "Edit an existing embed sent by the bot", "params": ["message_id", "title", "description", "color", "footer", "image", "thumbnail"]},
    "embed_send": {"desc": "Send a saved embed template to a channel", "params": ["name", "channel"]},
    "embed_list": {"desc": "List all saved embed templates", "params": []},
    "embed_delete_template": {"desc": "Delete a saved embed template", "params": ["name"]},
    "event_create": {"desc": "Create and launch a button race event", "params": ["title", "reward_type", "reward_value", "max_winners", "description", "channel", "duration_hours"]},
    "event_list": {"desc": "List recent events", "params": []},
    "rank": {"desc": "View your rank card", "params": ["member"]},
    "leaderboard": {"desc": "View the XP leaderboard", "params": []},
    "setxp": {"desc": "Set XP for a member (admin)", "params": ["member", "xp"]},
    "resetxp": {"desc": "Reset a member's XP and level back to 0 (admin)", "params": ["member"]},
    "resetleaderboard": {"desc": "Force an immediate leaderboard reset for this server (admin)", "params": []},
    "prestige": {"desc": "Prestige — reset past a level threshold for a permanent status tier", "params": []},
    "minigames_setup": {"desc": "Configure the Event Stack Builder (minigames)", "params": ["channel", "min_events", "max_events", "claim_seconds", "enabled"]},
    "minigames_tier_add": {"desc": "Add a reward tier for minigame spawns", "params": ["tier", "reward_type", "reward_value", "weight", "duration_hours"]},
    "minigames_tier_list": {"desc": "List configured minigame reward tiers", "params": []},
    "minigames_tier_remove": {"desc": "Remove a minigame reward tier by ID", "params": ["tier_id"]},
    "minigames_force": {"desc": "Force-spawn a minigame right now (admin, ignores probability)", "params": []},
    "minigames_stats": {"desc": "View this week's Event Stack Builder progress", "params": []},
    "missions": {"desc": "View your active missions and progress", "params": []},
    "mission_create": {"desc": "Create a mission (admin)", "params": ["name", "type", "target", "reward_type", "reward_value", "period", "description", "duration_hours"]},
    "mission_list": {"desc": "List configured missions (admin)", "params": []},
    "mission_remove": {"desc": "Remove a mission by ID (admin)", "params": ["mission_id"]},
    "kick": {"desc": "Kick a member from the server", "params": ["member", "reason"]},
    "ban": {"desc": "Ban a member from the server", "params": ["member", "reason", "delete_days", "duration"]},
    "unban": {"desc": "Unban a user by ID", "params": ["user_id", "reason"]},
    "timeout": {"desc": "Timeout a member for a duration", "params": ["member", "minutes", "reason"]},
    "untimeout": {"desc": "Remove a timeout from a member", "params": ["member", "reason"]},
    "warn": {"desc": "Warn a member", "params": ["member", "reason"]},
    "warnings": {"desc": "View warnings for a member", "params": ["member"]},
    "clearwarnings": {"desc": "Clear all warnings for a member", "params": ["member"]},
    "purge": {"desc": "Delete messages in bulk", "params": ["amount", "member"]},
    "lock": {"desc": "Lock a channel to prevent messages", "params": ["reason"]},
    "unlock": {"desc": "Unlock a previously locked channel", "params": ["reason"]},
    "slowmode": {"desc": "Set slowmode in a channel", "params": ["seconds"]},
    "modlogs": {"desc": "View moderation logs for a member", "params": ["member"]},
    "massban": {"desc": "Ban multiple users by ID at once", "params": ["user_ids", "reason"]},
    "lockdown": {"desc": "Lock all channels across the server", "params": ["reason"]},
    "unlockdown": {"desc": "Unlock all channels across the server", "params": []},
    "mvp_scores": {"desc": "View today's MVP scores", "params": []},
    "mvp_setup": {"desc": "Configure the MVP system", "params": ["mvp_role", "announce_channel", "cycle_hours", "chat_weight", "voice_weight"]},
    "mvp_force": {"desc": "Force a new MVP cycle immediately (admin)", "params": []},
    "reactionrole_create": {"desc": "Create a reaction role message with buttons", "params": ["channel", "title", "description", "exclusive", "max_roles", "require_confirmation"]},
    "reactionrole_add": {"desc": "Add a role button to a reaction role message", "params": ["message_id", "role", "label", "color", "emoji", "booster_only", "required_role", "expiry_days"]},
    "reactionrole_remove": {"desc": "Remove a role button from a reaction role message", "params": ["message_id", "role"]},
    "reactionrole_list": {"desc": "List all reaction role messages in the server", "params": []},
    "report_setup": {"desc": "Configure the user report system", "params": ["report_channel", "staff_role", "enabled"]},
    "report_list": {"desc": "View recent reports (staff only)", "params": ["status"]},
    "schedule_message": {"desc": "Schedule a message to be sent later (UTC times)", "params": ["channel", "message", "when", "repeat", "repeat_interval"]},
    "schedule_list": {"desc": "List this server's scheduled messages", "params": []},
    "schedule_cancel": {"desc": "Cancel a scheduled message by ID", "params": ["message_id"]},
    "shop": {"desc": "View the server shop items", "params": []},
    "inventory": {"desc": "View your purchased items and inventory", "params": []},
    "sticky_set": {"desc": "Set a sticky message in a channel", "params": ["channel", "content"]},
    "sticky_remove": {"desc": "Remove the sticky message from a channel", "params": ["channel"]},
    "sticky_list": {"desc": "List all sticky messages in the server", "params": []},
    "ticket_setup": {"desc": "Set up the ticket support system", "params": ["channel", "staff_role", "log_channel", "ticket_category", "categories"]},
    "ticket_add": {"desc": "Add a member to the current ticket", "params": ["member"]},
    "ticket_remove": {"desc": "Remove a member from the current ticket", "params": ["member"]},
    "ticket_close": {"desc": "Close the current ticket", "params": []},
    "trade": {"desc": "Start a trade with another member", "params": ["member"]},
    "trade_history": {"desc": "View your recent trade history", "params": ["member"]},
    "trigger_add": {"desc": "Add an auto-response trigger", "params": ["trigger", "response"]},
    "trigger_remove": {"desc": "Remove a trigger by ID", "params": ["trigger_id"]},
    "trigger_list": {"desc": "List all active triggers", "params": []},
    "trigger_toggle": {"desc": "Enable or disable a trigger", "params": ["trigger_id"]},
    "twitch_setup": {"desc": "Set up Twitch stream live alerts", "params": ["twitch_username", "discord_channel", "ping_role", "give_role", "discord_streamer", "custom_message"]},
    "twitch_remove": {"desc": "Remove a Twitch stream alert", "params": ["entry_id"]},
    "twitch_list": {"desc": "List configured Twitch alert configs", "params": []},
    "welcome_setup": {"desc": "Send the welcome / rules gate embed", "params": ["channel"]},
    "welcome_test": {"desc": "Test the welcome message for yourself", "params": []},
    "youtube_setup": {"desc": "Add a YouTube channel to watch for uploads", "params": ["youtube_url", "discord_channel", "ping_role", "custom_message"]},
    "youtube_remove": {"desc": "Remove a YouTube notification", "params": ["entry_id"]},
    "youtube_list": {"desc": "List YouTube notification configs", "params": []},
}


@app.route("/commands")
@require_page("commands")
def commands_dashboard():
    guild_id = get_session_guild_id()

    def _has_items(val):
        if not val:
            return False
        try:
            arr = json.loads(val) if isinstance(val, str) else val
            return bool(arr)
        except Exception:
            return False

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
                    "has_restrictions": (
                        _has_items(r[6]) or _has_items(r[7]) or
                        _has_items(r[8]) or _has_items(r[9]) or
                        _has_items(r[2]) or _has_items(r[3])
                    ),
                }
            return result

    toggles = run_async(get_toggles())
    ctx     = get_current_user_context()

    # Calculate summary stats for the commands dashboard
    all_cmds = [cmd for cmds in COMMAND_CATEGORIES.values() for cmd in cmds]
    total_count = len(all_cmds)
    disabled_count = sum(1 for cmd in all_cmds if toggles.get(cmd, {}).get("enabled", 1) == 0)
    enabled_count = total_count - disabled_count
    restricted_count = sum(1 for cmd in all_cmds if toggles.get(cmd, {}).get("has_restrictions"))

    stats = {
        "total": total_count,
        "enabled": enabled_count,
        "disabled": disabled_count,
        "restricted": restricted_count,
        "categories": len(COMMAND_CATEGORIES),
    }

    return render("manage/commands.html",
                  categories=COMMAND_CATEGORIES,
                  metadata=COMMAND_METADATA,
                  toggles=toggles,
                  stats=stats,
                  **ctx)


@app.route("/config/commands", methods=["GET", "POST"])
@require_page("commands")
def config_commands():
    return redirect(url_for("commands_dashboard"))


# ── Commands API ───────────────────────────────────────────────────────────────

@app.route("/api/commands/toggle", methods=["POST"])
@require_page("commands")
def api_command_toggle():
    guild_id = get_session_guild_id()
    data     = request.json or {}
    command  = (data.get("command") or "").strip().lstrip("/")
    if not command:
        return jsonify({"success": False, "error": "Command name is required"}), 400
    enabled  = int(bool(data.get("enabled", True)))

    async def toggle():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO command_toggles (guild_id, command_name, enabled, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, command_name)
                DO UPDATE SET enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP
            """, (guild_id, command, enabled))
            await db.commit()

    run_async(toggle())
    log_action(guild_id,
               f"{'Enabled' if enabled else 'Disabled'} /{command}", "commands")
    return jsonify({"success": True, "command": command, "enabled": bool(enabled)})


@app.route("/api/commands/bulk-toggle", methods=["POST"])
@require_page("commands")
def api_commands_bulk_toggle():
    guild_id = get_session_guild_id()
    data     = request.json or {}
    commands = data.get("commands", [])
    enabled  = int(bool(data.get("enabled", True)))
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
                    INSERT INTO command_toggles (guild_id, command_name, enabled, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(guild_id, command_name)
                    DO UPDATE SET enabled=excluded.enabled,
                                  updated_at=CURRENT_TIMESTAMP
                """, (guild_id, cmd, enabled))
            await db.commit()

    run_async(bulk())
    desc = f"in category '{category}'" if category else "globally"
    log_action(guild_id,
               f"Bulk {'enabled' if enabled else 'disabled'} {len(commands)} commands {desc}",
               "commands")
    return jsonify({"success": True, "count": len(commands), "enabled": bool(enabled)})


@app.route("/api/commands/settings/<command>", methods=["GET"])
@require_page("commands")
def api_command_settings_get(command: str):
    guild_id = get_session_guild_id()
    cmd_clean = command.strip().lstrip("/")

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
            """, (guild_id, cmd_clean))
            row = await cur.fetchone()
            if not row:
                return {"command_name": cmd_clean, "enabled": 1}
            res = dict(zip([d[0] for d in cur.description], row))
            if not res.get("enabled_roles") and res.get("allowed_roles"):
                res["enabled_roles"] = res["allowed_roles"]
            if not res.get("enabled_channels") and res.get("allowed_channels"):
                res["enabled_channels"] = res["allowed_channels"]
            return res

    return jsonify(run_async(get()))



# ── Alias advice (dashboard side, never a veto) ─────────────────────────
#
# Which system answers a given message is decided at runtime by
# utils/message_router.py — prefix command > alias > custom command > trigger,
# exactly one winner — and that answer is final. So the dashboard does not get
# to refuse a save because a word is used elsewhere: it gets to *explain* what
# will happen instead.
#
# This is a change of behaviour on purpose. The code before it raised
# ValueError (HTTP 400) for five different "conflicts", which both contradicted
# the router and blocked setups that work fine — e.g. alias `k` next to a
# trigger word `k` is not an error, it just means the trigger stops hearing
# messages that *begin* with `k`. Format problems (spaces, punctuation, length)
# stay hard errors, because those words can never work at all.

ALIAS_MAX_LEN = 32
#: commands the prefix parser owns no matter what (main.py registers them;
#: the bot process, not this Flask app, is the authority, so this is a hint
#: list rather than a source of truth)
ALIAS_BUILTINS = {"sync", "reload", "help"}


def _json_word_list(raw):
    """Parse a JSON array column into normalized alias words."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return normalize_aliases(parsed)


def normalize_aliases(raw):
    """Strip, lowercase, dedupe — and drop the `!`/`/` people paste in."""
    out = []
    for item in raw or []:
        if not isinstance(item, str):
            continue
        word = item.strip().lower().lstrip("!/")
        if word and word not in out:
            out.append(word)
    return out


def alias_format_error(alias: str):
    """Hard rules only — collisions are not checked here on purpose."""
    if not alias:
        return "An alias can't be empty."
    if len(alias) > ALIAS_MAX_LEN:
        return (f"Alias '{alias}' is too long — {ALIAS_MAX_LEN} characters "
                f"maximum.")
    if any(ch.isspace() for ch in alias):
        return f"Alias '{alias}' can't contain spaces — one word per alias."
    if not alias.replace("-", "").replace("_", "").isalnum():
        return (f"Alias '{alias}' is invalid — use only letters, numbers, "
                f"hyphens and underscores.")
    return None


async def alias_warnings(db, guild_id, command_name, aliases):
    """Non-blocking notes about what these alias words will collide with.

    Takes the open aiosqlite connection so a save doesn't need a second one.
    """
    if not aliases:
        return []

    words = list(aliases)
    notes = []

    def note(kind, alias, message):
        notes.append({"kind": kind, "alias": alias, "message": message})

    for alias in words:
        if alias == command_name:
            note("own-name", alias,
                 f"'{alias}' is already the name of /{command_name}, so the "
                 f"alias adds nothing and Nero ignores it. An alias has to be "
                 f"a different word.")
        if alias in ALIAS_BUILTINS:
            note("prefix", alias,
                 f"'!{alias}' is one of Nero's own commands and keeps that "
                 f"form: prefixed input always goes to it. Bare "
                 f"'{alias}' will still run /{command_name}.")
        if alias in COMMAND_METADATA and alias != command_name:
            note("slash-name", alias,
                 f"'{alias}' is also a command name. Typing it without a "
                 f"prefix now runs /{command_name}; the slash menu entry "
                 f"/{alias} is unaffected.")

    cursor = await db.execute("""
        SELECT guild_id, command_name, aliases FROM command_toggles
        WHERE (guild_id = ? OR guild_id = 0)
          AND aliases IS NOT NULL AND aliases != '[]' AND aliases != ''
    """, (guild_id,))
    for row_guild, other_cmd, aliases_json in await cursor.fetchall():
        try:
            same_row = (int(row_guild) == int(guild_id)
                        and other_cmd == command_name)
        except (TypeError, ValueError):
            same_row = False
        if same_row:
            continue
        for alias in _json_word_list(aliases_json):
            if alias not in words:
                continue
            if int(row_guild or 0) == 0:
                note("global", alias,
                     f"'{alias}' is also stored on the global (guild 0) row "
                     f"for /{other_cmd}. Command settings are read per server, "
                     f"so that row has no effect in a real server — set the "
                     f"alias on this server's row, as you are doing now.")
            else:
                note("duplicate", alias,
                     f"'{alias}' is currently the alias of /{other_cmd} here. "
                     f"Saving moves it to /{command_name} and /{other_cmd} "
                     f"stops answering to it — the most recently saved row "
                     f"owns the word.")

    cursor = await db.execute("""
        SELECT trigger FROM custom_commands
        WHERE (guild_id = ? OR guild_id = 0)
    """, (guild_id,))
    for (trigger,) in await cursor.fetchall():
        token = (trigger or "").strip().lower().lstrip("!")
        if token not in words:
            continue
        note("custom", token,
             f"A custom command '!{token}' also exists. Both keep working — "
             f"'{token}' runs /{command_name}, '!{token}' runs the custom "
             f"command — because aliases are typed bare and have no !-form.")

    cursor = await db.execute("""
        SELECT trigger_words, match_type, fuzzy_match, fuzzy_threshold
        FROM triggers
        WHERE (guild_id = ? OR guild_id = 0) AND enabled = 1
    """, (guild_id,))
    rows = await cursor.fetchall()
    for tw_raw, match_type, fuzzy, threshold in rows:
        for word in [w.strip().lower() for w in (tw_raw or "").split(",")]:
            if not word:
                continue
            for alias in words:
                if word == alias:
                    note("trigger", alias,
                         f"A trigger listens for the whole message "
                         f"'{word}'. One message can only run one thing, and "
                         f"aliases win — so a message that *starts* with "
                         f"'{alias}' now runs /{command_name} instead of "
                         f"getting the auto-reply. Rename the trigger if you "
                         f"want that reply back.")
                elif match_type == "contains" and (alias in word or
                                                    word in alias):
                    note("trigger-substring", alias,
                         f"The trigger '{word}' matches anywhere in a message, "
                         f"so it still fires for most sentences — but a message "
                         f"that starts with '{alias}' runs /{command_name} "
                         f"first (aliases take priority).")
                elif match_type == "startswith" and (
                        word.startswith(alias) or alias.startswith(word)):
                    note("trigger-substring", alias,
                         f"The trigger '{word}' matches the start of a message "
                         f"and overlaps '{alias}'; whichever the router claims "
                         f"first wins, and aliases outrank triggers.")
                elif fuzzy:
                    try:
                        from thefuzz import fuzz
                        ratio = fuzz.partial_ratio(alias, word)
                    except Exception:
                        ratio = 0
                    if ratio >= int(threshold or 80) and alias != word:
                        note("trigger-fuzzy", alias,
                             f"The fuzzy trigger '{word}' scores {ratio} "
                             f"against '{alias}' (threshold "
                             f"{int(threshold or 80)}). The alias wins for "
                             f"messages that begin with '{alias}'; the trigger "
                             f"keeps answering elsewhere.")
    return notes


@app.route("/api/commands/alias-registry")
@require_page("commands")
def api_command_alias_registry():
    """The stored alias map for this guild, for the chip input to annotate.

    Read-only, and deliberately narrow: it reports what is in the database and
    what the bot last reported about building its index. Runtime precedence
    stays in the bot (utils/message_router.py) — the labels below are the only
    place this file mentions it, so the two cannot disagree about behaviour.
    """
    guild_id = get_session_guild_id()

    async def read():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT guild_id, command_name, aliases, enabled, updated_at
                FROM command_toggles
                WHERE (guild_id = ? OR guild_id = 0)
                  AND aliases IS NOT NULL AND aliases != '[]' AND aliases != ''
                ORDER BY updated_at DESC, id DESC
            """, (guild_id,))
            entries, shadowed = {}, []
            for row_guild, cmd_name, aliases_json, enabled, updated in \
                    await cursor.fetchall():
                for alias in _json_word_list(aliases_json):
                    if alias in entries:
                        # newest row already owns it, so this is the loser
                        shadowed.append({
                            "alias": alias, "command": cmd_name,
                            "guild_id": int(row_guild or 0),
                            "reason": "shadowed-by-newer-save",
                        })
                        continue
                    entries[alias] = {
                        "command": cmd_name,
                        "guild_id": int(row_guild or 0),
                        "scope": "global" if int(row_guild or 0) == 0
                        else "server",
                        "enabled": 1 if enabled else 0,
                        "updated_at": str(updated or ""),
                    }

            cursor = await db.execute(
                "SELECT trigger FROM custom_commands "
                "WHERE guild_id = ? OR guild_id = 0", (guild_id,))
            custom = sorted({(t or "").strip().lower().lstrip("!")
                             for (t,) in await cursor.fetchall()
                             if (t or "").strip()})

            cursor = await db.execute(
                "SELECT trigger_words FROM triggers "
                "WHERE (guild_id = ? OR guild_id = 0) AND enabled = 1",
                (guild_id,))
            trigger_words = sorted({
                w.strip().lower()
                for (tw,) in await cursor.fetchall() if tw
                for w in str(tw).split(",") if w.strip()
            })

            cursor = await db.execute(
                "SELECT value FROM bot_settings "
                "WHERE key = 'command_aliases_sync_needed'")
            flag = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT value FROM bot_settings "
                "WHERE key = 'command_aliases_last_sync'")
            last = await cursor.fetchone()

        try:
            last_sync = json.loads(last[0]) if last else None
        except (TypeError, ValueError):
            last_sync = {"raw": last[0] if last else None}
        return entries, shadowed, custom, trigger_words, flag, last_sync

    (entries, shadowed, custom, trigger_words, flag,
     last_sync) = run_async(read())

    return jsonify({
        "success": True,
        "guild_id": guild_id,
        "aliases": entries,
        "shadowed": shadowed,
        "custom_commands": custom,
        "trigger_words": trigger_words,
        "prefix_commands": sorted(ALIAS_BUILTINS),
        "slash_commands": sorted(COMMAND_METADATA),
        "pending_resync": bool(flag and flag[0] == "1"),
        "last_sync": last_sync,
    })


@app.route("/api/commands/settings/<command>", methods=["POST"])
@require_page("commands")
def api_command_settings_save(command: str):
    guild_id  = get_session_guild_id()
    data      = request.json or {}
    cmd_clean = command.strip().lstrip("/")
    if not cmd_clean:
        return jsonify({"success": False, "error": "Invalid command name"}), 400

    has_enabled = "enabled" in data
    enabled_val = int(bool(data["enabled"])) if has_enabled else None

    en_roles  = json.dumps(data.get("enabled_roles", [])) if data.get("enabled_roles") else None
    dis_roles = json.dumps(data.get("disabled_roles", [])) if data.get("disabled_roles") else None
    en_chans  = json.dumps(data.get("enabled_channels", [])) if data.get("enabled_channels") else None
    dis_chans = json.dumps(data.get("disabled_channels", [])) if data.get("disabled_channels") else None

    allow_roles = en_roles
    allow_chans = en_chans

    cooldown_raw = data.get("cooldown_seconds")
    cooldown_sec = int(cooldown_raw) if cooldown_raw is not None and str(cooldown_raw).lstrip("-").isdigit() and int(cooldown_raw) >= 0 else None

    # Aliases: the *format* is enforced, because a word containing a space or
    # punctuation can never match; overlap with other systems is reported as a
    # warning further down instead of blocking the save. Note there is
    # deliberately no minimum length — `k` is the single most requested alias.
    aliases_raw = normalize_aliases(data.get("aliases", []))
    for alias in aliases_raw:
        problem = alias_format_error(alias)
        if problem:
            return jsonify({"success": False, "error": problem}), 400

    aliases = json.dumps(aliases_raw) if aliases_raw else None
    save_warnings = []

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            # Collected while the row is still in its previous state, so
            # "move this word off /other" reads correctly.
            save_warnings.extend(
                await alias_warnings(db, guild_id, cmd_clean, aliases_raw))

            await db.execute("""
                INSERT INTO command_toggles
                    (guild_id, command_name, enabled, allowed_roles,
                     allowed_channels, cooldown_seconds, aliases,
                     enabled_roles, disabled_roles, enabled_channels,
                     disabled_channels, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, command_name) DO UPDATE SET
                    enabled = CASE WHEN ? IS NOT NULL THEN excluded.enabled ELSE command_toggles.enabled END,
                    allowed_roles = excluded.allowed_roles,
                    allowed_channels = excluded.allowed_channels,
                    cooldown_seconds = excluded.cooldown_seconds,
                    aliases = excluded.aliases,
                    enabled_roles = excluded.enabled_roles,
                    disabled_roles = excluded.disabled_roles,
                    enabled_channels = excluded.enabled_channels,
                    disabled_channels = excluded.disabled_channels,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                guild_id, cmd_clean,
                enabled_val if has_enabled else 1,
                allow_roles, allow_chans,
                cooldown_sec, aliases,
                en_roles, dis_roles, en_chans, dis_chans,
                enabled_val,
            ))

            # Signal the bot to re-sync aliases
            await db.execute("""
                INSERT INTO bot_settings (key, value)
                VALUES ('command_aliases_sync_needed', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1'
            """)
            await db.commit()

    try:
        run_async(save())
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    log_action(guild_id, f"Updated settings for /{cmd_clean}", "commands")
    return jsonify({"success": True, "command": cmd_clean,
                    "warnings": save_warnings})


@app.route("/api/commands/bulk-restrict", methods=["POST"])
@require_page("commands")
def api_commands_bulk_restrict():
    guild_id = get_session_guild_id()
    data     = request.json or {}
    category = data.get("category")

    if not category or category not in COMMAND_CATEGORIES:
        return jsonify({"success": False, "error": "Invalid category"}), 400

    commands = COMMAND_CATEGORIES[category]

    has_en_roles  = "enabled_roles" in data
    has_dis_roles = "disabled_roles" in data
    has_en_chans  = "enabled_channels" in data
    has_dis_chans = "disabled_channels" in data

    en_roles  = json.dumps(data.get("enabled_roles", [])) if data.get("enabled_roles") else None
    dis_roles = json.dumps(data.get("disabled_roles", [])) if data.get("disabled_roles") else None
    en_chans  = json.dumps(data.get("enabled_channels", [])) if data.get("enabled_channels") else None
    dis_chans = json.dumps(data.get("disabled_channels", [])) if data.get("disabled_channels") else None

    async def bulk_restrict():
        async with aiosqlite.connect(DB_PATH) as db:
            for cmd in commands:
                await db.execute("""
                    INSERT INTO command_toggles (guild_id, command_name, enabled, updated_at)
                    VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(guild_id, command_name) DO NOTHING
                """, (guild_id, cmd))

                updates = []
                params = []
                if has_en_roles:
                    updates.append("enabled_roles = ?, allowed_roles = ?")
                    params.extend([en_roles, en_roles])
                if has_dis_roles:
                    updates.append("disabled_roles = ?")
                    params.append(dis_roles)
                if has_en_chans:
                    updates.append("enabled_channels = ?, allowed_channels = ?")
                    params.extend([en_chans, en_chans])
                if has_dis_chans:
                    updates.append("disabled_channels = ?")
                    params.append(dis_chans)

                if updates:
                    updates.append("updated_at = CURRENT_TIMESTAMP")
                    sql = f"UPDATE command_toggles SET {', '.join(updates)} WHERE guild_id = ? AND command_name = ?"
                    params.extend([guild_id, cmd])
                    await db.execute(sql, tuple(params))

            await db.commit()

    run_async(bulk_restrict())
    log_action(guild_id,
               f"Updated restrictions for category '{category}' ({len(commands)} commands)",
               "commands")
    return jsonify({"success": True, "category": category, "count": len(commands)})


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
        submitted_token = request.form.get("csrf_token", "")
        if not submitted_token or submitted_token != session.get("csrf_token"):
            abort(403)

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

    async def resolve():
        from utils.discord_user_cache import resolve_users
        ids = [u[1] for u in users]
        if not ids:
            return {}
        return await resolve_users(guild_id, ids)

    user_map = run_async(resolve())
    ctx      = get_current_user_context()
    return render("config/access.html", users=users, user_map=user_map, **ctx)


# ── Member edit API ────────────────────────────────────────────────────────────

@app.route("/api/edit-member", methods=["POST"])
@require_page("members_edit")
def api_edit_member():
    guild_id  = get_session_guild_id()
    data      = request.json
    user_id   = data.get("user_id")
    xp        = max(0, int(data.get("xp", 0)))
    coins     = max(0, int(data.get("coins", 0)))
    diamonds  = max(0, int(data.get("diamonds", 0)))
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
                INSERT INTO economy (guild_id, user_id, balance, diamonds)
                VALUES (?,?,?,?)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET balance=?, diamonds=?
            """, (guild_id, user_id, coins, diamonds, coins, diamonds))
            await db.commit()

    run_async(update())
    log_action(guild_id, f"Edited member {user_id}: xp={xp} coins={coins} diamonds={diamonds}",
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

    # ADVISORY ONLY (2026-08-28): a trigger word that is also an alias used to
    # be rejected outright. It can't be: the router already settles it (an
    # alias claims only messages that *start* with that word, and only when the
    # word isn't a prefix command or custom command). Saving anyway is fine —
    # the save response says what will change, and the triggers page shows it.
    trigger_warnings = []
    trigger_words_raw = data.get("trigger_words", "")
    if trigger_words_raw:
        new_words = [w.strip().lower() for w in str(trigger_words_raw).split(",")
                     if w.strip()]

        async def alias_note_for_words():
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("""
                    SELECT guild_id, command_name, aliases FROM command_toggles
                    WHERE (guild_id = ? OR guild_id = 0)
                      AND aliases IS NOT NULL AND aliases != '[]' AND aliases != ''
                """, (guild_id,))
                owners = {}
                for row_guild, cmd_name, aliases_json in await cursor.fetchall():
                    for alias in _json_word_list(aliases_json):
                        owners.setdefault(alias, (cmd_name, int(row_guild or 0)))
            out = []
            for word in new_words:
                owner = owners.get(word)
                if not owner:
                    continue
                cmd_name, row_guild = owner
                scope = ("on the global (guild 0) row" if row_guild == 0
                         else "in this server")
                out.append({
                    "kind": "alias",
                    "alias": word,
                    "message": (
                        f"'{word}' is an alias {scope} for /{cmd_name}. "
                        f"Messages that start with '{word}' run that command "
                        f"instead of this trigger; the trigger still answers "
                        f"everywhere else."),
                })
            return out

        trigger_warnings = run_async(alias_note_for_words())

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO triggers
                    (guild_id, trigger_words, response_text,
                     response_embed, response_type, match_type,
                     fuzzy_match, fuzzy_threshold, case_sensitive,
                     response_chance, cooldown_seconds,
                     allowed_channels, enabled)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)
            """, (
                guild_id,
                data.get("trigger_words"),
                data.get("response_text"),
                json.dumps(data.get("response_embed")) if data.get("response_embed") else None,
                data.get("response_type", "text"),
                data.get("match_type", "contains"),
                int(data.get("fuzzy_match", 0)),
                int(data.get("fuzzy_threshold", 80)),
                int(data.get("case_sensitive", 0)),
                int(data.get("response_chance", 100)),
                int(data.get("cooldown_seconds", 0)),
                json.dumps(data.get("allowed_channels", [])),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Added trigger: {data.get('trigger_words')}", "triggers")
    return jsonify({"success": True, "warnings": trigger_warnings})


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

    # ADVISORY ONLY, same reasoning as /api/save-trigger: the router decides
    # who answers, the dashboard explains. `enabled` is written here too, since
    # the bot now honours it (a row with enabled=0 is genuinely off — before
    # this, the column was ignored and "disabled" commands kept replying).
    trigger_warnings = []
    cc_token = normalize_aliases([data.get("trigger", "")])
    if cc_token:
        async def alias_note_for_token():
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("""
                    SELECT guild_id, command_name, aliases FROM command_toggles
                    WHERE (guild_id = ? OR guild_id = 0)
                      AND aliases IS NOT NULL AND aliases != '[]' AND aliases != ''
                """, (guild_id,))
                owners = {}
                for row_guild, cmd_name, aliases_json in await cursor.fetchall():
                    for alias in _json_word_list(aliases_json):
                        owners.setdefault(alias, (cmd_name, int(row_guild or 0)))
            alias, row_guild = owners.get(cc_token[0], (None, None))
            if alias is None:
                return []
            return [{
                "kind": "alias",
                "alias": cc_token[0],
                "message": (
                    f"'{cc_token[0]}' is also an alias for /{alias} "
                    f"{'on the global (guild 0) row' if row_guild == 0 else 'in this server'}. "
                    f"They do not clash — '!{cc_token[0]}' runs this custom "
                    f"command, bare '{cc_token[0]}' runs /{alias} — but it is "
                    f"worth knowing, because aliases have no !-form."),
            }]

        trigger_warnings = run_async(alias_note_for_token())

    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO custom_commands
                    (guild_id, trigger, allowed_roles, actions,
                     embed_title, embed_description, embed_color,
                     log_channel_id, same_channel, dm_member,
                     dm_message, requires_mention, requires_reason,
                     enabled)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                int(bool(data.get("enabled", True))),
            ))
            await db.commit()

    run_async(save())
    log_action(guild_id, f"Added custom command: !{data.get('trigger')}",
               "customcommands")
    return jsonify({"success": True, "warnings": trigger_warnings})


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

import os

if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
