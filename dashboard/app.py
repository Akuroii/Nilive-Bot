from flask import Flask, redirect, url_for, session, request, render_template
import requests
import os
import aiosqlite
import asyncio
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
                (guild_id, trigger, response, embed_title, embed_color, input_channel_id, output_channel_id)
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
                "SELECT command FROM disabled_commands WHERE guild_id=?",
                (0,))
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
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
