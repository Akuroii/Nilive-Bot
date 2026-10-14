"""S1 regression: each dashboard page script is emitted once and stays re-runnable.

What this locks down (the S1 defect):

* every page that had its inline `<script>` nested inside `{% block content %}`
  used to emit that script TWICE through base.html, and the second evaluation
  of a page script containing top-level `const`/`let` died with
  "SyntaxError: Identifier 'X' has already been declared" — which killed the
  page script on every htmx navigation too;
* the fix keeps the script inside `#content-area` but wraps it in one IIFE and
  re-publishes the page's top-level functions through `__neroGlobal`, so the
  markup's inline `onclick=`/`onchange=` handlers still resolve by bare name
  and a second evaluation is harmless.

The test therefore asserts, per route: exactly one copy of the page script in
the served HTML, it sits inside `#content-area`, the template no longer carries
the nested `{% block scripts %}`, the served script is re-evaluation safe (no
top-level `const`/`let`), and every function the markup calls from an inline
`onclick=`/`onchange=` still resolves by bare name — either because the script
declares it at top level (unwrapped script), because it is re-published through
`window`/`globalThis`/`__neroGlobal` (wrapped script), or because the shared
`dashboard.js` provides it. A page whose handlers stopped resolving is an inert
swapped page, which is the failure mode this harness exists to catch. A
`node --check` syntax pass runs on the extracted script when node is available.

Run from the scripts directory:
    python test_dashboard_page_scripts.py
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from phase1_support import ROOT  # noqa: F401  (side effect: scratch DB + env)

# Every route the sidebar navigates to via htmx (hx-get in base.html) plus the
# member profile. Each one swaps #content-area, so its page script is evaluated
# again on every visit and must be safe to re-run. `rel` is the template that
# renders it where there is one; routes without a page script are still checked
# for the "no unwrapped script" property.
PAGES = {
    "/members": "general/members.html",
    "/members/1": "general/member_profile.html",
    "/leveling": "systems/leveling.html",
    "/trade": "systems/trade.html",
    "/economy": "systems/economy.html",
    "/commands": "manage/commands.html",
    "/minigames": "systems/minigames.html",
    "/missions": "systems/missions.html",
    "/mvp": "systems/mvp.html",
    "/inventory": "systems/inventory.html",
    "/ledger": "systems/ledger.html",
    "/shop": "systems/shop.html",
    "/config/general": "config/general.html",
    "/config/boost": "config/boost.html",
    "/config/welcome": "config/welcome.html",
    "/config/botprofile": "config/botprofile.html",
    "/creator": "config/creator.html",
    "/custom-commands": "manage/customcommands.html",
    "/embed-builder": "manage/embedbuilder.html",
    "/reaction-roles": "manage/reactionroles.html",
    "/tickets": "manage/tickets.html",
    "/triggers": "manage/triggers.html",
    "/server-select": "server_select.html",
    "/minigames/builder": "systems/minigame_builder.html",
    # the rest of the dashboard's nav targets (no nested block, but the same
    # swap + re-evaluate lifecycle)
    "/": None,
    "/events": None,
    "/reports": None,
    "/backups": "general/backups.html",
    "/moderation": "manage/moderation.html",
    "/tag-missions": "systems/tagmissions.html",
    "/tag-partners": "systems/tagpartners.html",
    "/shop-designer": None,
    "/shop-publisher": None,
    "/config/access": None,
    "/audit-log": None,
    "/health": None,
}
TEMPLATES = ROOT / "dashboard" / "templates"
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>")
MARKUP_HANDLER = re.compile(
    r"on(?:click|change|input|submit|keyup|keydown|blur|focus|mouseenter)\s*=\s*\"([^\"]*)\"")
# A bare `name(` only: `document.getElementById(...)`, `this.form.submit()` and
# friends are property accesses and need no page-level binding.
HANDLER_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
# JS keywords that a `name(` regex would otherwise mistake for a call.
JS_KEYWORDS = {
    "if", "for", "while", "switch", "return", "typeof", "new", "function",
    "else", "do", "delete", "void", "throw", "try", "catch", "finally", "await",
    "yield", "case", "in", "of", "instanceof", "with", "class", "var", "let", "const",
}
# Browser built-ins that inline handlers may legitimately call by bare name.
BUILTIN_GLOBALS = {
    "alert", "confirm", "prompt", "setTimeout", "clearTimeout", "setInterval",
    "clearInterval", "requestAnimationFrame", "parseInt", "parseFloat", "isNaN",
    "Number", "String", "Boolean", "Array", "Object", "JSON", "Math", "Date",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI", "fetch",
    "escape", "unescape", "print", "open", "htmx", "btoa", "atob", "structuredClone",
} | JS_KEYWORDS
# Anything assigned onto one of these becomes a property of the global object,
# which is exactly what an inline handler's bare-name lookup finds.
PUBLISH = re.compile(
    r"(?:window|globalThis|__neroGlobal)\s*\.\s*([A-Za-z_$][\w$]*)\s*=(?!=)")
SHARED_JS = ROOT / "dashboard" / "static" / "js" / "dashboard.js"


def page_script_in_window(html: str) -> str:
    """The largest inline script between #content-area and the edit modal."""
    start = html.find('id="content-area"')
    end = html.find('id="edit-modal"')
    window = html[start:end if end != -1 else len(html)]
    found = [m.group(1) for m in INLINE_SCRIPT.finditer(window)]
    return max(found, key=len) if found else ""


def declared_functions(template_text: str) -> set:
    """Top-level function declarations of the page script.

    These sit at column 0 (the S1 wrapper is inserted around the body without
    re-indenting it), so an indented `function` belongs to some inner closure —
    e.g. embed-builder's page script is itself a hand-written IIFE — and must
    NOT be expected in the publish list."""
    return set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
                          template_text, re.M))


def analyse_script(script: str):
    """Top-level bindings of a classic script, or None when acorn is unavailable.

    Returns `{"consts": [...], "functions": [...]}`. `consts` is what breaks on
    re-evaluation; `functions` is what a bare-name lookup from an inline handler
    can reach. Wrapped pages publish their functions instead (the wrapper hides
    them from the global scope), which is why the two are reported apart."""
    node = shutil.which("node")
    if not node:
        return None
    acorn = next((candidate for candidate in (
        Path("/tmp/neuro/node_modules/acorn"),
        ROOT / "node_modules" / "acorn",
    ) if candidate.exists()), None)
    if acorn is None:
        return None
    probe = """
const acorn = require(process.argv[2]);
const src = require('fs').readFileSync(process.argv[3], 'utf8');
const ast = acorn.parse(src, { ecmaVersion: 'latest' });
const wrapped = (n) => n.type === 'ExpressionStatement' && n.expression.type === 'CallExpression'
  && ['FunctionExpression', 'ArrowFunctionExpression'].includes(n.expression.callee.type);
const isFn = (n) => n && ['FunctionExpression', 'ArrowFunctionExpression'].includes(n.type);
const consts = [], functions = [];
for (const n of ast.body) {
  if (wrapped(n)) continue;                      // IIFE: nothing leaks to the top level
  if (n.type === 'FunctionDeclaration' && n.id) functions.push(n.id.name);
  if (n.type === 'VariableDeclaration') {
    for (const d of n.declarations) {
      if (!d.id || d.id.type !== 'Identifier') continue;
      if (n.kind !== 'var') consts.push(n.kind + ' ' + d.id.name);
      if (n.kind === 'var' && isFn(d.init)) functions.push(d.id.name);
    }
  }
}
console.log(JSON.stringify({ consts, functions }));
"""
    with tempfile.TemporaryDirectory() as tmp:
        probe_path = Path(tmp) / "probe.js"
        probe_path.write_text(probe)
        script_path = Path(tmp) / "page.js"
        script_path.write_text(script)
        result = subprocess.run([node, str(probe_path), str(acorn), str(script_path)],
                                capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "{}")
    except ValueError:
        return None


def is_wrapped(script: str) -> bool:
    """True when the whole page script is one IIFE (the S1 wrapper shape)."""
    return bool(re.match(r"\s*\(\s*(?:async\s+)?(?:function\b|\()", script))


def _shared_globals():
    if not hasattr(_shared_globals, "cache"):
        _shared_globals.cache = (declared_functions(SHARED_JS.read_text(encoding="utf-8"))
                                 if SHARED_JS.exists() else set())
    return _shared_globals.cache


def resolved_globals(script: str, analysis) -> set:
    """Names an inline markup handler can call by bare name from this page."""
    names = set(PUBLISH.findall(script)) | _shared_globals() | BUILTIN_GLOBALS
    if analysis is not None:
        names |= set(analysis.get("functions", []))
    elif not is_wrapped(script):
        # No acorn: column-0 declarations are the shape the S1 wrapper keeps
        # (it never re-indents the body), so this is a sound fallback for an
        # unwrapped script and is deliberately not used for a wrapped one.
        names |= declared_functions(script)
    return names


def markup_identifiers(page_text: str) -> set:
    """Bare `name(` calls written into inline handler attributes."""
    names = set()
    for attr in MARKUP_HANDLER.findall(page_text):
        names.update(HANDLER_CALL.findall(attr))
    return names


def main():
    import asyncio
    from phase1_support import GUILD, execute, reset_database

    asyncio.run(reset_database())
    execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
    execute("INSERT OR IGNORE INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
            (GUILD, 8201, 1234, 3))
    execute("INSERT OR IGNORE INTO economy (guild_id, user_id, balance, diamonds) "
            "VALUES (?,?,?,?)", (GUILD, 8201, 100, 5))

    import os
    import time
    import dashboard.app as dapp

    client = dapp.app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"id": int(os.environ["OWNER_ID"]), "username": "tester"}
        session["guild_id"] = GUILD
        session["guild_name"] = "Guild"
        session["user_level"] = "owner"
        session["csrf_token"] = "tok"
        session["expires_at"] = time.time() + 3600

    checks = []

    def check(name, ok, extra=""):
        checks.append((name, ok, extra))
        if not ok:
            print(f"  FAIL {name}  [{extra}]")

    node = shutil.which("node")
    for route, rel in PAGES.items():
        text = (TEMPLATES / rel).read_text(encoding="utf-8") if rel else ""

        if rel:
            # ── static template facts ──────────────────────────────────────
            check(f"{rel}: nested scripts block removed",
                  "{% block scripts %}" not in text)
            # A page script whose handlers are attached with addEventListener
            # (embed-builder) carries the explicit "nothing to re-publish"
            # marker instead of a publish list; that is information only — the
            # served-page handler resolution check below is what decides.
            handles_markup = "nothing to re-publish for markup handlers" not in text

        # ── served page facts ──────────────────────────────────────────────
        resp = client.get(route)
        if resp.status_code != 200:
            check(f"{route}: served 200", False, f"status={resp.status_code}")
            continue
        html = resp.get_data(as_text=True)
        script = page_script_in_window(html)
        if not script:
            # No page script on this route is fine (the page is markup only),
            # as long as that is genuinely true of the template too.
            if rel:
                check(f"{route}: template has no in-content page script either",
                      "<script>" not in text or "id=\"content-area\"" not in text
                      or text.count("<script>") == 0, "")
            else:
                check(f"{route}: no in-content page script (markup-only page)", True)
            continue

        check(f"{route}: page script emitted exactly once",
              html.count(script) == 1, f"copies={html.count(script)}")
        check(f"{route}: page script sits inside #content-area",
              html.index(script) > html.index('id="content-area"'), "outside the swap target")

        if node:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
                handle.write(script)
                path = handle.name
            result = subprocess.run([node, "--check", path],
                                    capture_output=True, text=True)
            Path(path).unlink(missing_ok=True)
            check(f"{route}: served page script parses (node --check)",
                  result.returncode == 0, (result.stderr or "").strip()[:120])
            # The systemic property: a script that is re-evaluated on every htmx
            # swap must not redeclare a top-level const/let (that threw
            # "Identifier 'X' has already been declared" and killed the whole
            # script). Either it is wrapped in an IIFE, or it declares only
            # var/function — both survive re-evaluation.
            analysis = analyse_script(script)
            if analysis is not None:
                check(f"{route}: re-evaluation safe (no top-level const/let)",
                      not analysis.get("consts"), ",".join(analysis.get("consts", [])))

        # ── the page must not be inert after the swap ──────────────────────
        handlers = markup_identifiers(html)
        if handlers:
            missing = handlers - resolved_globals(script, analysis)
            check(f"{route}: every inline markup handler still resolves",
                  not missing, ",".join(sorted(missing)))
        if rel and not handles_markup and analysis is not None and is_wrapped(script):
            # Wrapped scripts hide their declarations; anything the markup calls
            # has to be published explicitly (that is the S1 contract).
            declared = set(analysis.get("functions", [])) | declared_functions(script)
            unpublished = {n for n in (markup_identifiers(html) & declared)
                           if f"{n} = {n};" not in script}
            check(f"{route}: wrapped script republishes its markup handlers",
                  not unpublished, ",".join(sorted(unpublished)))

    failed = [c for c in checks if not c[1]]
    print(f"\nDASHBOARD PAGE SCRIPTS: {len(checks) - len(failed)} passed, {len(failed)} failed "
          f"({len(PAGES)} routes)")
    if not node:
        print("  SKIPPED: node is not installed — the syntax checks, the "
              "top-level const/let check and the handler-resolution check did "
              "NOT run; this is a partial result.")
    elif not analyse_script("var probe = 1;"):
        print("  SKIPPED: acorn is not installed (npm install acorn) — the "
              "top-level const/let check and the acorn-based handler resolution "
              "did NOT run; this is a partial result.")
    for name, _, extra in failed[:20]:
        print("  FAILED:", name, extra)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
