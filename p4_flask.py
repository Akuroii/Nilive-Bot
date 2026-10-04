"""PROBE 4 — real Flask dashboard routes, real SQLite, test client.

Verifies: Dashboard Save -> API -> DB write -> sync flag; the alias *format*
rules stay hard while *collisions* are advisory (2026-08-28 redesign); the
registry endpoint the chip input reads; and that the page + its new JS/CSS
assets actually render.

Run:  python tools/alias_probes/p4_flask.py
"""
import os, sys, json, time, pathlib, sqlite3

DBDIR = os.environ.get("PROBE_DIR", "/tmp/probes")
os.makedirs(DBDIR, exist_ok=True)
os.environ["DATABASE_PATH"] = os.path.join(DBDIR, "dash.db")
os.environ["NERO_ENVIRONMENT"] = "test"
os.environ["OWNER_ID"] = "704453350384730237"
os.environ["SECRET_KEY"] = "x" * 48
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DB = pathlib.Path(os.environ["DATABASE_PATH"])
if DB.exists():
    DB.unlink()

from dashboard.app import app          # noqa: E402

GUILD = 333
OWNER = 704453350384730237
HDRS = {"Content-Type": "application/json", "X-CSRF-Token": "probe-csrf"}

FAILURES = []
CHECKS = 0


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
          f"{('  -> ' + str(detail)[:150]) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def conn():
    return sqlite3.connect(DB)


def dump_aliases(cmd="kick"):
    with conn() as c:
        return c.execute(
            "SELECT guild_id, command_name, enabled, aliases FROM command_toggles "
            "WHERE command_name=?", (cmd,)).fetchall()


def flag():
    with conn() as c:
        return dict(c.execute(
            "SELECT key, value FROM bot_settings WHERE key LIKE '%alias%'").fetchall())


def settings_post(cl, cmd, aliases, **extra):
    payload = {"enabled": True, "aliases": aliases, "cooldown_seconds": None,
               "enabled_roles": [], "disabled_roles": [],
               "enabled_channels": [], "disabled_channels": []}
    payload.update(extra)
    return cl.post(f"/api/commands/settings/{cmd}", headers=HDRS,
                   data=json.dumps(payload))


def main():
    app.config["TESTING"] = True
    with app.test_client() as cl:
        with cl.session_transaction() as s:
            s["user"] = {"id": str(OWNER), "username": "owner", "avatar": None}
            s["expires_at"] = time.time() + 3600
            s["csrf_token"] = "probe-csrf"
            s["guild_id"] = GUILD
            s["guild_name"] = "probe"

        print("\n=== 1. saving a single-character alias ===")
        r = settings_post(cl, "kick", ["k"])
        body = r.get_json() or {}
        check("POST /api/commands/settings/kick accepts `k` (no min length)",
              r.status_code == 200 and body.get("success"), body)
        row = dump_aliases()[0]
        check("DB stores the alias as a JSON array", json.loads(row[3]) == ["k"], row)
        check("sync flag raised for the bot", flag().get("command_aliases_sync_needed") == "1",
              flag())
        check("success payload carries `warnings` (possibly empty)",
              isinstance(body.get("warnings"), list), body.get("warnings"))

        print("\n=== 2. the settings GET the panel renders ===")
        g = cl.get("/api/commands/settings/kick", headers={"X-CSRF-Token": "probe-csrf"})
        got = g.get_json() or {}
        check("GET returns the aliases for the chips to render",
              got.get("aliases") in ('["k"]', ["k"]), got.get("aliases"))

        print("\n=== 3. format errors stay 400, collisions become warnings ===")
        cases = [
            ("punctuation `k!`",            ["k!"],      "reject"),
            ("space `my alias`",            ["my alias"], "reject"),
            ("40 characters",               ["a" * 40], "reject"),
            ("Arabic single char `ض`",        ["ض"],      "accept"),
            ("underscore `k_x`",            ["k_x"],    "accept"),
            ("uppercase `K` normalises",    ["K"],      "accept"),
            ("own command name `kick`",     ["kick"],   "warn"),
            ("another command's name `ban`", ["ban"],   "warn"),
            ("Nero built-in `help`",        ["help"],   "warn"),
        ]
        for label, aliases, expectation in cases:
            r = settings_post(cl, "kick", aliases)
            body = r.get_json() or {}
            kinds = {w.get("kind") for w in (body.get("warnings") or [])}
            if expectation == "reject":
                ok = r.status_code == 400 and not body.get("success")
            elif expectation == "accept":
                ok = r.status_code == 200 and body.get("success")
            else:
                ok = r.status_code == 200 and body.get("success") and bool(kinds)
            check(f"{label:34s} -> {expectation}", ok,
                  (body.get("error") or sorted(kinds) or "no warning"))

        print("\n=== 4. a word already owned by another command is advisory ===")
        settings_post(cl, "ban", ["kk"])
        r = settings_post(cl, "kick", ["kk"])
        body = r.get_json() or {}
        dup = [w for w in (body.get("warnings") or []) if w.get("kind") == "duplicate"]
        check("duplicate alias saves (no 400) and warns which command loses it",
              body.get("success") and dup, dup or body)
        check("the warning names the command that will stop answering",
              dup and "/ban" in dup[0]["message"], dup[0]["message"] if dup else None)
        with conn() as c:
            c.execute("DELETE FROM command_toggles WHERE command_name='ban'")
            c.commit()

        print("\n=== 5. trigger + custom command words no longer block ===")
        settings_post(cl, "kick", ["k"])
        r = cl.post("/api/save-trigger", headers=HDRS, data=json.dumps({
            "trigger_words": "k", "response_text": "hi", "response_type": "text",
            "match_type": "contains", "fuzzy_match": 0, "case_sensitive": 0,
            "response_chance": 100, "cooldown_seconds": 0, "allowed_channels": []}))
        tb = r.get_json() or {}
        check("saving a trigger that shares a word with an alias succeeds",
              r.status_code == 200 and tb.get("success"), tb)
        check("…and explains what the alias will steal",
              any(w.get("kind") == "alias" for w in (tb.get("warnings") or [])),
              [w.get("message") for w in (tb.get("warnings") or [])])

        r = cl.post("/api/save-custom-command", headers=HDRS, data=json.dumps({
            "trigger": "k", "actions": [], "allowed_roles": [],
            "embed_title": "x", "embed_description": "y", "embed_color": "#ED4245",
            "same_channel": True, "dm_member": False, "dm_message": None,
            "requires_mention": True, "requires_reason": True}))
        cb = r.get_json() or {}
        check("saving custom command `!k` next to alias `k` succeeds",
              r.status_code == 200 and cb.get("success"), cb)
        check("…and says the two do not actually clash",
              any(w.get("kind") == "alias" for w in (cb.get("warnings") or [])),
              [w.get("message") for w in (cb.get("warnings") or [])])
        with conn() as c:
            enabled = c.execute(
                "SELECT enabled FROM custom_commands WHERE trigger='k'").fetchone()
        check("the new row is written with enabled honoured",
              enabled and int(enabled[0]) == 1, enabled)

        print("\n=== 6. registry endpoint the chip input reads ===")
        r = cl.get("/api/commands/alias-registry", headers={"X-CSRF-Token": "probe-csrf"})
        reg = r.get_json() or {}
        check("GET /api/commands/alias-registry is reachable", r.status_code == 200,
              r.status_code)
        entry = (reg.get("aliases") or {}).get("k") or {}
        check("registry maps the word to its command",
              entry.get("command") == "kick" and entry.get("scope") == "server", entry)
        check("registry lists the competing systems",
              "k" in (reg.get("trigger_words") or []) and
              "k" in (reg.get("custom_commands") or []),
              {"triggers": reg.get("trigger_words"), "custom": reg.get("custom_commands")})
        check("registry reports a pending resync while the flag is up",
              reg.get("pending_resync") is True, reg.get("pending_resync"))
        with conn() as c:
            c.execute("UPDATE bot_settings SET value='0' WHERE key='command_aliases_sync_needed'")
            c.execute("""INSERT INTO bot_settings (key, value)
                         VALUES ('command_aliases_last_sync',
                                 '{\"at\": 1, \"registered\": 1, \"skipped\": [], \"error\": null}')
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value""")
            c.commit()
        reg = (cl.get("/api/commands/alias-registry",
                      headers={"X-CSRF-Token": "probe-csrf"}).get_json() or {})
        check("registry exposes the bot's last sync result",
              (reg.get("last_sync") or {}).get("registered") == 1, reg.get("last_sync"))
        check("…and no longer claims a resync is pending",
              reg.get("pending_resync") is False, reg.get("pending_resync"))

        print("\n=== 7. clearing the aliases list removes them ===")
        r = settings_post(cl, "kick", [])
        row = dump_aliases()[0]
        check("empty list clears the column", body_ok(row), row)

        print("\n=== 8. page + assets render ===")
        page = cl.get("/commands", headers={"X-CSRF-Token": "probe-csrf"})
        html = page.get_data(as_text=True)
        check("commands page renders", page.status_code == 200, page.status_code)
        check("alias chip host is in the markup", 'data-alias-input' in html)
        check("chip host carries the command name for conflict labels",
              'data-alias-command="kick"' in html)
        check("the component is loaded", "nero-alias-input.js" in html)
        for asset in ("/static/js/nero-alias-input.js", "/static/css/main.css"):
            a = cl.get(asset)
            check(f"{asset} served", a.status_code == 200, a.status_code)
        css = cl.get("/static/css/main.css").get_data(as_text=True)
        check("chip styles shipped", ".na-chip" in css and ".na-box" in css)
        js = cl.get("/static/js/nero-alias-input.js").get_data(as_text=True)
        check("chip component shipped", "window.NeroAlias" in js and "' '" in js)
        check("the page no longer tells users to type a comma-separated list",
              "comma-separated" not in html)
        check("the alias status line is rendered", 'id="alias-status"' in html)
        check("…and is refreshed from the registry",
              "refreshAliasStatus" in html and "/api/commands/alias-registry" in html)


def body_ok(row):
    return (row[3] or "") in ("", "null", "None") or row[3] is None


main()
print(f"\n{'='*60}")
if FAILURES:
    print("FAILED:", *FAILURES, sep="\n  - ")
    sys.exit(1)
print(f"{CHECKS}/{CHECKS} checks passed")
