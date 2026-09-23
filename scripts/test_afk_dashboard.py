"""Offline checks for the AFK Commands-page integration (dashboard/button pass).

No Flask, no Jinja, no Discord connection:

* ``dashboard/app.py``'s ``COMMAND_CATEGORIES`` / ``COMMAND_METADATA`` are
  literal dicts — parsed with ``ast.literal_eval`` from the module source.
* ``manage/commands.html`` assertions target the exact Jinja expressions the
  AFK pass added (row name, usage line, alias hides), and a tiny pure-Python
  renderer mirrors those two edited expressions to verify rendered output
  for both flag values.

Covers:

    afk lives under the "Utility & Trade" category (and nowhere else)
    metadata marks it a prefix command with params ["reason"]
    usage renders exactly "!afk [reason]" (prefix) while other commands keep
        their "/cmd [param]" rendering
    alias usage-hint and alias editor are hidden for prefix commands
    the dashboard asks only one thing of the bot: a "afk" command_toggles
        row (the same architecture every other command uses)

Run:  python3 scripts/test_afk_dashboard.py   (stdlib only)
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY = os.path.join(ROOT, "dashboard", "app.py")
COMMANDS_HTML = os.path.join(ROOT, "dashboard", "templates", "manage",
                             "commands.html")

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def literal_dict(source: str, name: str) -> dict:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} assignment not found")


def main():
    app_source = open(APP_PY, encoding="utf-8").read()
    html = open(COMMANDS_HTML, encoding="utf-8").read()

    print("[1] commands registry")
    categories = literal_dict(app_source, "COMMAND_CATEGORIES")
    metadata = literal_dict(app_source, "COMMAND_METADATA")

    check("afk is registered under 'Utility & Trade'",
          "afk" in categories.get("Utility & Trade", []))
    check("afk appears in exactly one category",
          sum("afk" in cmds for cmds in categories.values()) == 1)

    meta = metadata.get("afk", {})
    check("afk metadata exists", bool(meta))
    check("afk is marked a prefix command", meta.get("prefix") is True)
    check("afk params are exactly ['reason']",
          meta.get("params") == ["reason"])
    check("afk description names the prefix command",
          "afk" in (meta.get("desc") or "").lower())

    print("[2] template renders prefix vs slash")
    row_expr = "{{ '!' if meta.prefix else '/' }}{{ cmd }}"
    check("row name uses the prefix-aware expression",
          f'<span class="cmd-row-name">{row_expr}</span>' in html)
    usage_expr_html = (
        "<code>{{ '!' if meta.prefix else '/' }}{{ cmd }}"
        "{% for param in meta.params|default([]) %} [{{ param }}]{% endfor %}"
        "</code>")
    check("usage line uses the prefix-aware expression",
          usage_expr_html in html)

    # Mirror exactly the two edited Jinja expressions in pure Python so the
    # rendered output can be asserted for both flag values.
    def render_usage(meta_dict, cmd):
        base = ("!" if meta_dict.get("prefix") else "/") + cmd
        return base + "".join(f" [{p}]" for p in meta_dict.get("params") or [])

    check("usage renders exactly '!afk [reason]'",
          render_usage(meta, "afk") == "!afk [reason]")
    check("slash commands keep old usage rendering",
          render_usage(metadata.get("kick", {}), "kick")
          == "/kick [member] [reason]")
    check("name renders exactly '!afk'",
          ("!" if meta.get("prefix") else "/") + "afk" == "!afk")

    print("[3] alias UI is not presented for prefix commands")
    hint_guard = re.search(
        r"{%\s*if not meta\.prefix\s*%}.*?cmd-usage-alias-hint.*?{%\s*endif\s*%}",
        html, re.S)
    check("alias usage-hint is hidden for prefix commands",
          hint_guard is not None)
    editor_guard = re.search(
        r"{%\s*if not meta\.prefix\s*%}.*?form-label\">Aliases</label>.*?{%\s*endif\s*%}",
        html, re.S)
    check("alias editor is hidden for prefix commands",
          editor_guard is not None)

    print("[4] shared toggle architecture only (no parallel AFK system)")
    check("no dedicated AFK route in the dashboard",
          '"/afk"' not in app_source and "'/afk'" not in app_source.replace("\n", ""))
    check("no AFK Systems template exists",
          not os.path.exists(os.path.join(ROOT, "dashboard", "templates",
                                          "systems", "afk.html")))
    check("no AFK API module exists",
          not os.path.exists(os.path.join(ROOT, "dashboard", "api", "afk.py")))
    check("no AFK nav entry in base.html",
          "afk" not in open(os.path.join(ROOT, "dashboard", "templates",
                                         "base.html"), encoding="utf-8")
                            .read().lower())

    print()
    color = ("\x1b[32m" if FAIL == 0 else "\x1b[31m")
    print(f"\x1b[1m{color}{PASS}/{PASS + FAIL} passed.\x1b[0m")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
