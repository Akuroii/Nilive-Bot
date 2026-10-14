"""PROBE 2 — static guard: the four defects must stay dead.

The runtime behaviour is proven in p8 (bot) and p4 (dashboard); this file is
cheap insurance against a refactor quietly reintroducing one of them, because
every single one of them survived an earlier "looks fine, compiles" review:

  1. rewriting ``message.content`` from an on_message listener
  2. registering aliases in ``bot.all_commands`` (leaked across every guild)
  3. ``slash_cmd.params`` / ``Parameter(converter=...)`` construction that
     raised and was swallowed by ``except Exception: pass``
  4. ``sc.callback(interaction, ...)`` without the cog binding
  5. ``SELECT *`` + a hardcoded 14-value unpack against custom_commands
  6. a minimum alias length (there must never be one — `k` is the point)
  7. two copies of the dashboard gating logic in main.py and the cog

Run:  python tools/alias_probes/p2_real_cog.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def read(rel):
    return (ROOT / rel).read_text()


FAILURES = []
CHECKS = 0


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}"
          f"{('  -> ' + str(detail)[:120]) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def main():
    aliases = read("cogs/command_aliases.py")
    router = read("utils/message_router.py")
    custom = read("cogs/customcommands.py")
    triggers = read("cogs/triggers.py")
    mainpy = read("main.py")
    app = read("dashboard/app.py")
    db = read("database.py")

    print("\n=== 1. no message mutation, no global registration ===")
    check("the alias cog never assigns to message.content",
          not re.search(r"message\.content\s*=", aliases),
          re.findall(r".*message\.content\s*=.*", aliases))
    check("the alias cog never writes into bot.all_commands",
          not re.search(r"all_commands\s*\[", aliases)
          and "add_command(" not in aliases, "found a registration")
    check("the alias cog reads aliases but never registers prefix commands",
          "get_context" in aliases and "bot.invoke" in aliases)
    check("router, not cog, owns precedence", "class MessageRouter" in router
          and "Route.ALIAS" in aliases)

    print("\n=== 2. no silent failure paths ===")
    lines = aliases.split("\n")

    def func_at(lineno):
        for i in range(lineno, -1, -1):
            m = re.match(r"    (?:async )?def (\w+)", lines[i])
            if m:
                return m.group(1)
        return "?"

    def func_text(name):
        out = []
        grab = False
        for line in lines:
            if re.match(r"    (?:async )?def " + re.escape(name) + r"\b", line):
                grab = True
            elif grab and re.match(r"    (?:async )?def \w+", line):
                break
            elif grab and re.match(r"    # ── ", line):
                break
            if grab:
                out.append(line)
        return "\n".join(out)

    sync_src = func_text("_sync_aliases")
    naked = [i for i, ln in enumerate(sync_src.split("\n"))
             if ln.strip().startswith("except ") and
             sync_src.split("\n")[i + 1].strip() == "pass"]
    check("every failure in _sync_aliases is recorded (nothing nakedly passed)",
          not naked and sync_src.count("except") <= sync_src.count("skipped.append"),
          {"excepts": sync_src.count("except"),
           "recorded": sync_src.count("skipped.append"), "naked": len(naked)})
    check("the poll logs a failure and leaves the flag set for a retry",
          "sync check failed (will retry)" in aliases)
    swallow_funcs = []
    for i, line in enumerate(lines):
        if re.match(r"\s*except Exception:\s*$", line) and i + 1 < len(lines) \
                and lines[i + 1].strip() == "pass":
            swallow_funcs.append(func_at(i))
    check(f"the only blanket swallows left are teardown and the can't-reply "
          f"path (found: {sorted(set(swallow_funcs))})",
          set(swallow_funcs) <= {"cog_unload", "_invoke_alias"},
          sorted(set(swallow_funcs)))

    check("sync problems are recorded, not swallowed",
          "_record_sync_status" in aliases and "command_aliases_last_sync" in aliases)

    print("\n=== 3. the app-command call convention is the 2.7.1 one ===")
    check("slash callbacks are called with their binding",
          re.search(r"sc\.callback\(\s*binding", aliases) is not None
          or "sc.callback(binding, interaction" in aliases)
    check("no `sc.cog` read on an app_command (AttributeError in 2.7.1)",
          ".cog" not in re.sub(r"#.*", "", aliases))
    build = aliases[aliases.index("def build_alias_command"):]
    check("params are built through commands.Parameter(annotation=...)",
          "commands.Parameter(" in build and "annotation=converter" in build
          and not re.search(r"commands\.Parameter\([^)]*converter=", build, re.S))
    check("KEYWORD_ONLY is what marks the trailing consume-rest param",
          "KEYWORD_ONLY" in aliases)

    print("\n=== 4. custom_commands read cannot drift from the schema ===")
    check("no SELECT * against custom_commands",
          re.search(r"SELECT \*\s+FROM", custom) is None
          and re.search(r"SELECT \*\s+FROM", router) is None)
    m = re.search(r"CUSTOM_COMMAND_COLUMNS\s*=\s*\((.*?)\n\)", router, re.S)
    parts = re.findall(r'"([^"]*)"', m.group(1)) if m else []
    cols = [c.strip() for chunk in parts for c in chunk.split(",") if c.strip()]
    check("router declares an explicit column list", len(cols) >= 13, cols)
    unpack = re.search(r"\((id_, guild_id,[^)]*)\)\s*=\s*cmd", custom, re.S)
    names = [n.strip() for n in unpack.group(1).split(",")] if unpack else []
    # Position, not spelling: the cog calls column 6 `embed_desc` locally,
    # which is fine as long as the count and the leading names line up. That
    # count check is the whole point — a `SELECT *` growing two columns is
    # what made every message crash the unpack before the fix.
    check("the cog's unpack lines up with the column list",
          bool(names) and len(names) == len(cols) and names[1:5] == cols[1:5],
          {"names": len(names), "cols": len(cols),
           "zip": list(zip(names, cols))})
    check("enabled is filtered in the query", "enabled = 1" in router
          and "ADD COLUMN enabled" in custom)
    check("database.py really has those columns",
          all(c in db for c in ("requires_reason", "enabled")))

    print("\n=== 5. no alias length rule anywhere ===")
    for label, text in (("alias cog", aliases), ("router", router),
                        ("dashboard API", app)):
        check(f"{label}: no minimum-length rule",
              not re.search(r"len\(alias\)\s*<\s*[1-9]", text),
              re.findall(r".*len\(alias\)\s*<.*", text))
    check("dashboard documents that single characters are allowed",
          "no minimum length" in app.lower() or "deliberately no minimum length" in app.lower())
    check("the UI field has no minlength",
          "minlength" not in read("dashboard/templates/manage/commands.html")
          or "alias" not in read("dashboard/templates/manage/commands.html").split("minlength")[1][:120])

    print("\n=== 6. aliases are bare-only, so `!word` cannot double-run ===")
    check("router refuses alias lookup for !-prefixed tokens",
          re.search(r'startswith\("!"\)', router) is not None)
    check("custom commands are only matched for !-prefixed tokens",
          re.search(r'first\.startswith\("!"\)', router) is not None)

    print("\n=== 7. one gate implementation ===")
    gating = read("utils/command_gating.py")
    check("shared module exists", "def evaluate_toggle_row" in gating)
    check("the cog uses it", "from utils.command_gating import" in aliases)
    check("main.py uses it", "evaluate_toggle_row" in mainpy
          and "from utils.command_gating import" in mainpy)
    i = mainpy.find("async def interaction_check")
    check("main.py no longer re-implements the JSON role/channel rules",
          i > 0 and "allowed_roles" not in mainpy[i:i + 4000],
          "interaction_check not found" if i < 0 else "still parses roles inline")
    check("triggers defer to the router", "Route.TRIGGER" in triggers)
    check("custom commands defer to the router", "Route.CUSTOM_COMMAND" in custom)
    check("main.py no longer claims the alias cog must load last",
          "Load order does NOT matter" in mainpy
          and not re.search(r"^\s*#\s*MUST be last", mainpy, re.M))

    print("\n=== 8. dashboard conflicts are advisory ===")
    check("save endpoint returns warnings", '"warnings": save_warnings' in app)
    check("the old hard-block raises are gone from the alias section",
          "conflicts with the slash command" not in app)
    check("trigger save warns instead of rejecting",
          "conflict with" not in app and "trigger_warnings" in app)
    check("registry endpoint exists", "/api/commands/alias-registry" in app)

    print(f"\n{'='*60}")
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} static guards hold")
    if FAILURES:
        print("FAILED:", *FAILURES, sep="\n  - ")
        sys.exit(1)


main()
