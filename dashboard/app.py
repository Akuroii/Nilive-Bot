from flask import Flask, redirect, url_for, session, request, render_template, jsonify
import requests
import os
import aiosqlite
import asyncio
import json
import math
from functools import wraps

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "nero-dashboard-secret-key-2024")

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DISCORD_API = "https://discord.com/api/v10"
DB_PATH = "nero.db"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

def calculate_level(xp):
    level = 0
    while xp >= math.floor(100 * ((level + 1) ** 1.5)):
        xp -= math.floor(100 * ((level + 1) ** 1.5))
        level += 1
    return level

@app.route("/")
@login_required
def index():
    return render_template("index.html", user=session["user"])

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/discord_login")
def discord_login():
    scope = "identify"
    return redirect(
        f"https://discord.com/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={scope}"
    )

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect(url_for("login"))
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(f"{DISCORD_API}/oauth2/token", data=data, headers=headers)
    tokens = r.json()
    access_token = tokens.get("access_token")
    if not access_token:
        return redirect(url_for("login"))
    user_r = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"})
    user = user_r.json()
    session["user"] = {
        "id": user.get("id"),
        "username": user.get("username"),
        "avatar": user.get("avatar"),
    }
    session["access_token"] = access_token
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/mvp")
@login_required
def mvp():
    async def get_mvp():
        from datetime import date
        today = date.today().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, message_score, voice_minutes, total_score
                FROM mvp_scores WHERE date=?
                ORDER BY total_score DESC LIMIT 20
            """, (today,))
            return await cursor.fetchall()
    rows = run_async(get_mvp())
    return render_template("mvp.html", user=session["user"], scores=rows)

@app.route("/leveling")
@login_required
def leveling():
    async def get_levels():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, xp, level FROM levels
                ORDER BY xp DESC LIMIT 20
            """)
            return await cursor.fetchall()
    rows = run_async(get_levels())
    return render_template("leveling.html", user=session["user"], levels=rows)

@app.route("/economy")
@login_required
def economy():
    async def get_economy():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, balance FROM economy
                ORDER BY balance DESC LIMIT 20
            """)
            return await cursor.fetchall()
    rows = run_async(get_economy())
    return render_template("economy.html", user=session["user"], balances=rows)

@app.route("/moderation")
@login_required
def moderation():
    async def get_warnings():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT user_id, reason, timestamp FROM warnings
                ORDER BY timestamp DESC LIMIT 50
            """)
            return await cursor.fetchall()
    try:
        rows = run_async(get_warnings())
    except:
        rows = []
    return render_template("moderation.html", user=session["user"], warnings=rows)

@app.route("/tickets")
@login_required
def tickets():
    async def get_tickets():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, channel_id, user_id, status, category, created_at
                FROM tickets ORDER BY created_at DESC LIMIT 50
            """)
            return await cursor.fetchall()
    try:
        rows = run_async(get_tickets())
    except:
        rows = []
    return render_template("tickets.html", user=session["user"], tickets=rows)

@app.route("/triggers", methods=["GET", "POST"])
@login_required
def triggers():
    async def get_triggers():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS triggers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    trigger TEXT,
                    response TEXT,
                    embed_title TEXT,
                    embed_color TEXT,
                    input_channel_id INTEGER,
                    output_channel_id INTEGER
                )
            """)
            await db.commit()
            cursor = await db.execute("SELECT * FROM triggers")
            return await cursor.fetchall()

    async def add_trigger(trigger, response, embed_title, embed_color, input_ch, output_ch):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO triggers
                (guild_id, trigger, response, embed_title, embed_color,
                 input_channel_id, output_channel_id)
                VALUES (0, ?, ?, ?, ?, ?, ?)
            """, (trigger, response, embed_title, embed_color, input_ch, output_ch))
            await db.commit()

    async def delete_trigger(trigger_id):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM triggers WHERE id=?", (trigger_id,))
            await db.commit()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            run_async(add_trigger(
                request.form.get("trigger"),
                request.form.get("response"),
                request.form.get("embed_title"),
                request.form.get("embed_color", "#5865F2"),
                request.form.get("input_channel_id") or None,
                request.form.get("output_channel_id") or None,
            ))
        elif action == "delete":
            run_async(delete_trigger(request.form.get("trigger_id")))
        return redirect(url_for("triggers"))

    rows = run_async(get_triggers())
    return render_template("triggers.html", user=session["user"], triggers=rows)

@app.route("/commands", methods=["GET", "POST"])
@login_required
def commands_page():
    all_commands = [
        "kick", "ban", "unban", "timeout", "untimeout",
        "warn", "warnings", "clearwarnings", "purge",
        "lock", "unlock", "slowmode", "modlogs"
    ]

    async def get_disabled():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS disabled_commands (
                    guild_id INTEGER,
                    command TEXT,
                    PRIMARY KEY (guild_id, command)
                )
            """)
            await db.commit()
            cursor = await db.execute(
                "SELECT command FROM disabled_commands WHERE guild_id=?", (0,))
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def toggle_command(command, enable):
        async with aiosqlite.connect(DB_PATH) as db:
            if enable:
                await db.execute(
                    "DELETE FROM disabled_commands WHERE guild_id=? AND command=?",
                    (0, command))
            else:
                await db.execute(
                    "INSERT OR IGNORE INTO disabled_commands (guild_id, command) VALUES (?, ?)",
                    (0, command))
            await db.commit()

    if request.method == "POST":
        command = request.form.get("command")
        action = request.form.get("action")
        if command and action:
            run_async(toggle_command(command, action == "enable"))
        return redirect(url_for("commands_page"))

    disabled = run_async(get_disabled())
    return render_template("commands.html",
                           user=session["user"],
                           all_commands=all_commands,
                           disabled=disabled)

@app.route("/embedbuilder")
@login_required
def embedbuilder():
    return render_template("embedbuilder.html", user=session["user"])

@app.route("/reactionroles")
@login_required
def reactionroles():
    return render_template("reactionroles.html", user=session["user"])

@app.route("/settings")
@login_required
def settings():
    async def get_settings():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.commit()
            cursor = await db.execute("SELECT key, value FROM bot_settings")
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}
    raw = run_async(get_settings())
    settings_data = {
        "mvp_role_id": raw.get("mvp_role_id", ""),
        "mvp_channel_id": raw.get("mvp_channel_id", ""),
        "reset_hours": int(raw.get("reset_hours", 24)),
        "boost_role1": raw.get("boost_role1", ""),
        "boost_role2": raw.get("boost_role2", ""),
        "boost_channel": raw.get("boost_channel", ""),
        "xp_per_word": int(raw.get("xp_per_word", 2)),
        "xp_max": int(raw.get("xp_max", 50)),
        "levelup_channel": raw.get("levelup_channel", ""),
        "staff_role": raw.get("staff_role", ""),
        "ticket_log": raw.get("ticket_log", ""),
        "ticket_categories": raw.get("ticket_categories",
                                      "General Support,Report,Ban Appeal,Other"),
        "daily_min": int(raw.get("daily_min", 100)),
        "daily_max": int(raw.get("daily_max", 300)),
        "work_min": int(raw.get("work_min", 20)),
        "work_max": int(raw.get("work_max", 80)),
        "welcome_channel": raw.get("welcome_channel", ""),
        "welcome_msg": raw.get("welcome_msg", ""),
        "welcome_color": raw.get("welcome_color", "#5865F2"),
    }
    saved = request.args.get("saved")
    return render_template("settings.html", user=session["user"],
                           settings=settings_data, saved=saved)

async def save_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)
        """)
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (key, str(value)))
        await db.commit()

@app.route("/settings/mvp", methods=["POST"])
@login_required
def settings_mvp():
    run_async(save_setting("mvp_role_id", request.form.get("mvp_role_id", "")))
    run_async(save_setting("mvp_channel_id", request.form.get("mvp_channel_id", "")))
    run_async(save_setting("reset_hours", request.form.get("reset_hours", "24")))
    return redirect(url_for("settings") + "?saved=1")

@app.route("/settings/boost", methods=["POST"])
@login_required
def settings_boost():
    run_async(save_setting("boost_role1", request.form.get("role1_id", "")))
    run_async(save_setting("boost_role2", request.form.get("role2_id", "")))
    run_async(save_setting("boost_channel", request.form.get("boost_channel_id", "")))
    return redirect(url_for("settings") + "?saved=1")

@app.route("/settings/leveling", methods=["POST"])
@login_required
def settings_leveling():
    run_async(save_setting("xp_per_word", request.form.get("xp_per_word", "2")))
    run_async(save_setting("xp_max", request.form.get("xp_max", "50")))
    run_async(save_setting("levelup_channel", request.form.get("levelup_channel", "")))
    return redirect(url_for("settings") + "?saved=1")

@app.route("/settings/tickets", methods=["POST"])
@login_required
def settings_tickets():
    run_async(save_setting("staff_role", request.form.get("staff_role_id", "")))
    run_async(save_setting("ticket_log", request.form.get("log_channel_id", "")))
    run_async(save_setting("ticket_categories", request.form.get("categories", "")))
    return redirect(url_for("settings") + "?saved=1")

@app.route("/settings/economy", methods=["POST"])
@login_required
def settings_economy():
    run_async(save_setting("daily_min", request.form.get("daily_min", "100")))
    run_async(save_setting("daily_max", request.form.get("daily_max", "300")))
    run_async(save_setting("work_min", request.form.get("work_min", "20")))
    run_async(save_setting("work_max", request.form.get("work_max", "80")))
    return redirect(url_for("settings") + "?saved=1")

@app.route("/settings/welcome", methods=["POST"])
@login_required
def settings_welcome():
    run_async(save_setting("welcome_channel", request.form.get("welcome_channel", "")))
    run_async(save_setting("welcome_msg", request.form.get("welcome_msg", "")))
    run_async(save_setting("welcome_color", request.form.get("welcome_color", "#5865F2")))
    return redirect(url_for("settings") + "?saved=1")

@app.route("/members")
@login_required
def members():
    async def get_members():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT l.user_id, l.xp, l.level, COALESCE(e.balance, 0) as coins
                FROM levels l
                LEFT JOIN economy e ON l.user_id = e.user_id
                ORDER BY l.xp DESC LIMIT 50
            """)
            rows = await cursor.fetchall()
            return [{"user_id": r[0], "xp": r[1], "level": r[2], "coins": r[3]}
                    for r in rows]
    try:
        member_list = run_async(get_members())
    except:
        member_list = []
    return render_template("members.html", user=session["user"], members=member_list)

@app.route("/api/edit_member", methods=["POST"])
@login_required
def api_edit_member():
    data = request.json
    user_id = data.get("user_id")
    xp = data.get("xp", 0)
    coins = data.get("coins", 0)
    new_level = calculate_level(xp)
    async def update():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO levels (guild_id, user_id, xp, level)
                VALUES (0, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=?, level=?
            """, (user_id, xp, new_level, xp, new_level))
            await db.execute("""
                INSERT INTO economy (guild_id, user_id, balance)
                VALUES (0, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET balance=?
            """, (user_id, coins, coins))
            await db.commit()
    run_async(update())
    return jsonify({"success": True})

@app.route("/api/save_embed_template", methods=["POST"])
@login_required
def api_save_embed_template():
    data = request.json
    name = data.get("name")
    embed = data.get("embed")
    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS embed_templates (
                    guild_id INTEGER, name TEXT, data TEXT,
                    PRIMARY KEY (guild_id, name)
                )
            """)
            await db.execute("""
                INSERT OR REPLACE INTO embed_templates (guild_id, name, data)
                VALUES (0, ?, ?)
            """, (name.lower(), json.dumps(embed)))
            await db.commit()
    run_async(save())
    return jsonify({"success": True})

@app.route("/api/embed_templates")
@login_required
def api_embed_templates():
    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS embed_templates (
                    guild_id INTEGER, name TEXT, data TEXT,
                    PRIMARY KEY (guild_id, name)
                )
            """)
            await db.commit()
            cursor = await db.execute(
                "SELECT name FROM embed_templates WHERE guild_id=0")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]
    try:
        templates = run_async(get())
    except:
        templates = []
    return jsonify({"templates": templates})

@app.route("/api/embed_template/<name>", methods=["GET", "DELETE"])
@login_required
def api_embed_template(name):
    if request.method == "DELETE":
        async def delete():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM embed_templates WHERE guild_id=0 AND name=?", (name,))
                await db.commit()
        run_async(delete())
        return jsonify({"success": True})
    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT data FROM embed_templates WHERE guild_id=0 AND name=?", (name,))
            row = await cursor.fetchone()
            return row[0] if row else None
    data = run_async(get())
    if not data:
        return jsonify({"template": None})
    return jsonify({"template": json.loads(data)})

@app.route("/api/send_embed", methods=["POST"])
@login_required
def api_send_embed():
    return jsonify({"success": False,
                    "error": "Use /embed_create in Discord instead!"})

@app.route("/api/save_rr_panel", methods=["POST"])
@login_required
def api_save_rr_panel():
    data = request.json
    async def save():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS rr_panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, description TEXT, color TEXT,
                    channel_id TEXT, buttons TEXT,
                    exclusive INTEGER DEFAULT 0,
                    max_roles INTEGER DEFAULT 0,
                    require_confirmation INTEGER DEFAULT 0,
                    required_role TEXT
                )
            """)
            await db.execute("""
                INSERT INTO rr_panels
                (title, description, color, channel_id, buttons,
                 exclusive, max_roles, require_confirmation, required_role)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("title"),
                data.get("desc"),
                data.get("color"),
                data.get("channel"),
                json.dumps(data.get("buttons", [])),
                int(data.get("exclusive", 0)),
                int(data.get("max_roles", 0)),
                int(data.get("require_confirmation", False)),
                data.get("required_role", "")
            ))
            await db.commit()
    run_async(save())
    return jsonify({"success": True})

@app.route("/api/rr_panels")
@login_required
def api_rr_panels():
    async def get():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS rr_panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, description TEXT, color TEXT,
                    channel_id TEXT, buttons TEXT,
                    exclusive INTEGER DEFAULT 0,
                    max_roles INTEGER DEFAULT 0,
                    require_confirmation INTEGER DEFAULT 0,
                    required_role TEXT
                )
            """)
            await db.commit()
            cursor = await db.execute(
                "SELECT id, title, buttons FROM rr_panels ORDER BY id DESC")
            rows = await cursor.fetchall()
            return [{"id": r[0], "title": r[1],
                     "buttons": len(json.loads(r[2])) if r[2] else 0}
                    for r in rows]
    try:
        panels = run_async(get())
    except:
        panels = []
    return jsonify({"panels": panels})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
