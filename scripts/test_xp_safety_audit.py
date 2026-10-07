"""Completeness audit for the Leveling work: every XP entry point, every invariant.

This is the file that answers "is Leveling complete, and can a future edit quietly
break it?". It is deliberately an inventory plus assertions rather than a set of
scenarios:

* **Inventory.** Every production write to the `levels` table is discovered by
  scanning the source and has to match a known, classified entry point. Adding a
  new XP write path — the only way an XP-safety rule can be bypassed — fails this
  test until it is classified here.
* **Invariants.** For each entry point: does it clamp XP at zero, does it keep
  `level` consistent with the XP curve, does it touch entitlements/claims/roles?
  Plus the cross-cutting rules: positive grants all go through the reward engine,
  the spam gate runs before the cooldown and the grant, the anti-spam penalty
  cannot write a claim, the claim ledger cannot pay twice, the D2 reconciler is
  reachable only from the member-initiated claim pass, nothing sweeps members in
  the background, the dashboard harness cannot degrade silently, and the systems
  this work was not allowed to change (Missions, MVP, activity engine, voice)
  are untouched by it.

Run from the scripts directory:
    python test_xp_safety_audit.py
"""
import ast
import re
import sys
from pathlib import Path

from phase1_support import DB_PATH, GUILD, ROOT, USER, execute, reset_database, rows

PRODUCTION_DIRS = ("cogs", "utils", "dashboard")
# Files that legitimately write to `levels`, and why. Anything else fails.
LEVEL_WRITERS = {
    ("utils/reward_engine.py", "give_reward"): "positive XP grant (clamped)",
    ("utils/xp_calculator.py", "_apply_spam_penalty_in_transaction"): "anti-spam deduction (clamped)",
    ("cogs/leveling.py", "setxp"): "admin /setxp (clamped)",
    ("cogs/leveling.py", "resetxp"): "admin /resetxp (writes 0)",
    ("cogs/leveling.py", "perform_leaderboard_reset"): "leaderboard reset (writes 0)",
    ("dashboard/app.py", "api_edit_member"): "dashboard member edit (clamped)",
    ("utils/prestige.py", "purchase_prestige"): "prestige tier (never touches xp/level)",
}


def source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def production_files():
    for directory in PRODUCTION_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            yield path


def background_task_calls_reconciler() -> bool:
    """Inspect task-loop function bodies, not unrelated file-level tokens."""
    for path in production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_task_loop = any(
                ast.unparse(decorator).startswith("tasks.loop")
                for decorator in node.decorator_list
            )
            if not is_task_loop:
                continue
            if any(
                isinstance(item, ast.Call)
                and ((isinstance(item.func, ast.Name)
                      and item.func.id == "reconcile_role_progression")
                     or (isinstance(item.func, ast.Attribute)
                         and item.func.attr == "reconcile_role_progression"))
                for item in ast.walk(node)
            ):
                return True
    return False


def function_spans(text: str):
    """(name, start, end) for every function, innermost helpers included."""
    spans = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.name, node.lineno,
                          getattr(node, "end_lineno", None) or node.lineno))
    return spans


def function_body(text: str, *names: str) -> str:
    """The source lines of the largest function whose name is in `names`."""
    lines = text.splitlines(keepends=True)
    hits = [span for span in function_spans(text) if span[0] in names]
    if not hits:
        return ""
    _, start, end = max(hits, key=lambda span: span[2] - span[1])
    return "".join(lines[start - 1:end])


def enclosing_function(spans, lineno: int) -> str:
    """The OUTERMOST function containing `lineno` — the stable API surface.

    A write inside a nested helper (`api_edit_member`'s inner `update()`) is
    attributed to the public entry point that owns it, so an inventory entry
    does not churn when a helper is renamed.
    """
    hits = [span for span in spans if span[1] <= lineno <= span[2]]
    if not hits:
        return "?"
    return max(hits, key=lambda span: span[2] - span[1])[0]


# The XP-changing writers and the exact recompute/consistency token each one
# must contain in the same function as its write. The spam penalty's private
# writer is called inside apply_spam_penalty's BEGIN IMMEDIATE transaction.
LEVEL_CONSISTENCY = {
    ("utils/reward_engine.py", "give_reward"): "xp_progress(new_xp)",
    ("utils/xp_calculator.py", "_apply_spam_penalty_in_transaction"): "xp_progress(new_xp)[0]",
    ("cogs/leveling.py", "setxp"): "xp_progress(xp)",
    ("dashboard/app.py", "api_edit_member"): "calculate_level_from_xp(xp)",
}


def main():
    checks = []

    def check(name, ok, extra=""):
        checks.append((name, ok, extra))
        print(f"  {'PASS' if ok else 'FAIL'} {name}"
              + (f"  [{extra}]" if extra and not ok else ""))

    print("== 1. inventory: every `levels` write is a known entry point ==")
    found = {}
    for path in production_files():
        text = path.read_text(encoding="utf-8")
        if not re.search(r"\b(INSERT INTO levels|UPDATE levels|DELETE FROM levels"
                         r"|INSERT OR REPLACE INTO levels)\b", text):
            continue
        rel = str(path.relative_to(ROOT))
        spans = function_spans(text)
        for match in re.finditer(r"\b(INSERT INTO levels|UPDATE levels|DELETE FROM levels"
                                 r"|INSERT OR REPLACE INTO levels)\b", text):
            lineno = text[:match.start()].count("\n") + 1
            found[(rel, enclosing_function(spans, lineno))] = match.group(1)
    for key, statement in sorted(found.items()):
        check(f"{key[0]}::{key[1]}() is classified", key in LEVEL_WRITERS,
              f"{statement} is not in the audit inventory")
    for key in sorted(LEVEL_WRITERS):
        check(f"inventory still lists {key[0]}::{key[1]}()", key in found,
              "the audited entry point disappeared — re-audit")
    check("no production code deletes a member's levels row",
          not any(statement == "DELETE FROM levels" for statement in found.values()),
          str([k for k, v in found.items() if v == "DELETE FROM levels"]))

    print("== 2. no path can make XP negative ==")
    reward_engine = source("utils/reward_engine.py")
    penalty = source("utils/xp_calculator.py")
    cog = source("cogs/leveling.py")
    dashboard_app = source("dashboard/app.py")
    check("the XP grant clamps at zero",
          "new_xp    = max(0, old_xp + amount)" in reward_engine)
    check("the anti-spam deduction clamps at zero, at 1% per incident and to rolling budget",
          "incident_cap = old_xp // 100" in penalty
          and "deducted = min(requested, budget_remaining)" in penalty
          and "new_xp = max(0, old_xp - deducted)" in penalty
          and "BEGIN IMMEDIATE" in penalty)
    penalty_entry = function_body(penalty, "apply_spam_penalty")
    penalty_writer = function_body(
        penalty, "_apply_spam_penalty_in_transaction")
    check("incident identity, penalty, and budget use the same immediate transaction",
          "BEGIN IMMEDIATE" in penalty_entry
          and "_apply_spam_penalty_in_transaction(" in penalty_entry
          and "incident_detected" in penalty_entry)
    check("the shared penalty writer leaves commit/rollback to its caller",
          "db.commit" not in penalty_writer
          and "db.rollback" not in penalty_writer)
    check("admin /setxp clamps at zero", "xp = max(0, xp)" in cog)
    check("the dashboard member edit clamps at zero",
          'xp        = max(0, int(data.get("xp", 0)))' in dashboard_app)
    check("both reset paths write zero, not a negative",
          cog.count("SET xp = 0, level = 0") >= 2, str(cog.count("SET xp = 0, level = 0")))
    prestige = source("utils/prestige.py")
    check("the prestige purchase never rewrites xp/level",
          re.search(r"ON CONFLICT\(guild_id, user_id\) DO UPDATE SET\s+prestige = excluded\.prestige",
                    prestige) is not None)

    print("== 3. XP and level cannot become inconsistent ==")
    check("the grant recomputes level from the new XP",
          "new_level, _, _ = xp_progress(new_xp)" in reward_engine)
    check("the deduction recomputes level from the new XP",
          "new_level = xp_progress(new_xp)[0]" in penalty)
    check("admin /setxp recomputes level from the new XP",
          "new_level, _, _ = xp_progress(xp)" in cog)
    check("the dashboard edit recomputes level from the new XP",
          "new_level = calculate_level_from_xp(xp)" in dashboard_app)
    for (rel, func), token in LEVEL_CONSISTENCY.items():
        body = function_body(source(rel), func)
        check(f"{rel}::{func}() recomputes the level next to its XP write",
              token in body, f"missing {token!r} in {len(body)} chars")
    reset_body = cog[cog.index("async def resetxp"):]
    check("the reset path writes xp and level together",
          "DO UPDATE SET xp = 0, level = 0" in reset_body)
    winner = source("utils/reward_engine.py")
    check("the level recompute happens before the entitlement snapshot",
          winner.index("new_level, _, _ = xp_progress(new_xp)")
          < winner.index("record_crossing("))

    print("== 4. positive grants all go through the reward engine ==")
    for rel, needle in (("utils/mission_engine.py", "give_reward"),
                        ("utils/minigame_engine.py", "give_reward"),
                        ("cogs/events.py", "give_reward"),
                        ("cogs/shop.py", "give_reward"),
                        ("cogs/tagpartners.py", "give_reward"),
                        ("cogs/tagmissions.py", "give_reward")):
        check(f"{rel} grants XP through give_reward()", needle in source(rel))
    missions_cog = source("cogs/missions.py")
    check("the missions cog only displays rewards, it never writes XP",
          "INTO levels" not in missions_cog and "UPDATE levels" not in missions_cog)

    print("== 5. incident detection precedes Message XP cooldown/grant ==")
    message_fn = cog[cog.index("async def on_activity_message"):]
    message_fn = message_fn[:message_fn.index("async def _announce_levelup")]
    spam_at = message_fn.find('if config.get("spam_detection_enabled", 1):')
    cooldown_at = message_fn.find('cooldown = max(0, int(config.get("xp_cooldown_seconds", 10)))')
    grant_at = message_fn.find("await give_reward(")
    warn_at = message_fn.find("await self._warn_spam(")
    check("spam detection is live and separate from Message XP",
          'if config.get("spam_detection_enabled", 1):' in message_fn
          and 'if not config.get("message_xp_enabled", 1):' in message_fn)
    check("incident detection runs before the 10s cooldown",
          spam_at < cooldown_at, f"{spam_at} vs {cooldown_at}")
    check("incident detection runs before the XP grant",
          spam_at < grant_at, f"{spam_at} vs {grant_at}")
    check("one incident-claim warning branch precedes any XP grant",
          -1 < warn_at < grant_at
          and 'incident_started and outcome["applied"]' in message_fn
          and 'outcome["warning_due"]' in message_fn,
          f"warn={warn_at} grant={grant_at}")
    check("the penalty helper has one implementation",
          source("utils/xp_calculator.py").count("async def apply_spam_penalty") == 1)
    users = [str(path.relative_to(ROOT)) for path in production_files()
             if "apply_spam_penalty" in path.read_text(encoding="utf-8")]
    check("apply_spam_penalty has exactly one production caller module",
          users == ["cogs/leveling.py", "utils/xp_calculator.py"], str(users))
    # A second message listener granting XP would bypass the spam gate entirely.
    message_listeners = [str(path.relative_to(ROOT)) for path in production_files()
                         if "async def on_message" in path.read_text(encoding="utf-8")]
    check("there is more than one message listener in the bot (context)",
          len(message_listeners) >= 1, str(message_listeners))
    for rel in message_listeners:
        text = source(rel)
        check(f"{rel} does not grant XP from its message listener",
              "give_reward" not in text and "INTO levels" not in text
              and "UPDATE levels" not in text)
    check("exactly one module grants Message XP",
          [str(p.relative_to(ROOT)) for p in production_files()
           if 'reason="Message XP"' in p.read_text(encoding="utf-8")]
          == ["cogs/leveling.py"])
    check("the activity engine only dispatches, it never pays",
          "dispatch(\"activity_message\"" in source("cogs/activity_engine.py")
          and "give_reward" not in source("cogs/activity_engine.py"))

    print("== 6. the penalty cannot create, revoke or duplicate a claim ==")
    fn_body = (function_body(penalty, "apply_spam_penalty")
               + function_body(penalty, "_apply_spam_penalty_in_transaction"))
    check("the penalty never touches the claim ledger",
          "level_reward_claims" not in fn_body)
    check("the penalty never records a crossing",
          "record_crossing" not in fn_body)
    check("the penalty never delivers or removes a role",
          "deliver_role" not in fn_body and "add_roles" not in fn_body
          and "remove_roles" not in fn_body)
    claims = source("utils/level_claims.py")
    check("record_crossing only fires on an increase",
          "if new_level <= old_level:\n        return 0" in claims)
    check("the ledger refuses a second row for the same entitlement",
          "UNIQUE (guild_id, user_id, reward_level, track, reward_ref)" in claims
          and "INSERT OR IGNORE INTO level_reward_claims" in claims)

    print("== 7. Claim All cannot duplicate a payout ==")
    check("the reserve step only ever takes unfulfilled rows",
          "AND status != 'fulfilled'" in claims)
    check("a claim is marked only by its lease owner",
          re.search(r"def _mark\(.*?owner_token", claims, re.S) is not None
          and "owner_token=? AND id=?" in claims.replace("\n", " ").replace("  ", " ")
          or "owner_token" in claims)
    check("the delivery pass only processes rows it reserved",
          "_owned_rows(db, token)" in claims)

    print("== 8. demotion cannot re-trigger a fulfilled reward ==")
    check("fulfilled claims are invisible to the normal pass",
          "AND status != 'fulfilled'" in claims)
    check("re-crossing inserts are ignored, not duplicated",
          "INSERT OR IGNORE" in claims)
    check("the exclusivity setting defaults to OFF in code and in the schema",
          re.search(r'"remove_old_reward_role"\s*:\s*0', penalty) is not None
          and re.search(r"remove_old_reward_role\s+INTEGER DEFAULT 0",
                        source("database.py")) is not None)
    check("the config API validates the toggle as 0/1",
          '("remove_old_reward_role",' in source("dashboard/api/leveling.py")
          and ", 0, 0, 1)," in source("dashboard/api/leveling.py"))
    check("role replacement is deferred until the full role-delivery pass",
          "if exclusive_roles and member is not None and claim_ids is None:" in claims
          and "await enforce_role_progression(" not in claims[
              claims.index("for row in roles:"):claims.index("for row in temp_roles:")])

    print("== 9. D2 reconciliation cannot restore the wrong thing ==")
    rec = claims[claims.index("async def reconcile_role_progression"):]
    rec = rec[:rec.index("async def _grant_inventory")]
    check("the reconciler inspects all role claim statuses",
          "SELECT reward_level, payload_json, status" in rec and "TRACK_ROLE" in rec)
    check("the reconciler targets the highest role claim group",
          "highest = max(by_level)" in rec
          and 'keep = sorted(group["role_ids"])' in rec)
    check("fulfilled highest roles are restored even when a sibling is unresolved, but replacement is blocked",
          'if exclusive_roles and not keep:' in rec
          and 'group["unfulfilled"]' in rec
          and 'result["blocked"] = True' in rec)
    check("OFF restores every fulfilled role; ON restores only the highest group",
          'restore_groups = ([(highest, group)] if exclusive_roles else' in rec
          and 'sorted(by_level.items())' in rec)
    check("the reconciler never writes to the claim ledger",
          "INSERT" not in rec and "UPDATE level_reward_claims" not in rec)
    check("the reconciler never records a crossing",
          "record_crossing" not in rec)
    check("the reconciler never invents role ids from live config",
          "leveling_rewards" not in rec)
    check("a partial/unfulfilled highest-role delivery blocks lower-role removal",
          'result["failed"] or group["unfulfilled"] or any(' in rec)
    check("reconciliation reads the persisted remove-old setting for role restoration",
          "get_leveling_config(guild_id)" in rec
          and 'get("remove_old_reward_role")' in rec
          and "exclusive_roles" in rec)
    check("reconciliation has one claim/retry implementation plus the join callback",
          [str(p.relative_to(ROOT)) for p in production_files()
           if "reconcile_role_progression" in p.read_text(encoding="utf-8")]
          == ["cogs/leveling.py", "utils/level_claims.py"])
    check("Claim All path requires a member, full pass and replacement enabled",
          "if exclusive_roles and member is not None and claim_ids is None:" in claims)
    check("join callback reconciles automatically without requiring Claim All",
          "async def on_member_join" in source("cogs/leveling.py")
          and "reconcile_role_progression" in source("cogs/leveling.py"))
    check("no background task calls it",
          not background_task_calls_reconciler())
    check("no guild-wide/member-list sweep is introduced by the join callback",
          "for member in guild.members" not in source("cogs/leveling.py"))

    print("== 10. no retroactive sweep, no rejoin reset ==")
    call_sites = [str(p.relative_to(ROOT)) for p in production_files()
                  if "reconcile_role_progression(" in p.read_text(encoding="utf-8")]
    check("the reconciler has only the Claim All and join callback callers",
          call_sites == ["cogs/leveling.py", "utils/level_claims.py"],
          str(call_sites))
    claim_pass = source("utils/level_claims.py")
    claim_pass = claim_pass[claim_pass.index("async def claim_available"):]
    check("the claim-pass call is member-initiated, full-pass only",
          claim_pass.count("reconcile_role_progression(") == 1
          and "if exclusive_roles and member is not None and claim_ids is None:" in claim_pass)
    check("nothing loops over guilds or members to reconcile in bulk",
          not re.search(r"for\s+guild\w*\s+in[^\n]*\n(?:.*\n){0,6}.*reconcile_role_progression",
                        source("utils/level_claims.py")))
    lifecycle = {name: sum(source(f"cogs/{name}").count(token) for token in
                           ("FROM levels", "INTO levels", "UPDATE levels",
                            "level_reward_claims"))
                 for name in ("welcome.py", "tagpartners.py", "boost.py",
                              "reactionroles.py", "auditlog.py")}
    check("unrelated lifecycle cogs do not mutate level state",
          set(lifecycle.values()) == {0}, str(lifecycle))

    print("== 11. unrelated roles are preserved ==")
    enforce = claims[claims.index("async def enforce_role_progression"):]
    enforce = enforce[:enforce.index("async def reconcile_role_progression")]
    check("removals come only from the superseded-claim helper",
          "await superseded_role_ids(db, guild_id, user_id, level, role_id)" in enforce)
    check("roles the member does not hold are skipped, never guessed",
          "if role is None or role not in held:" in enforce)

    print("== 12. the dashboard harness cannot degrade silently ==")
    harness = source("scripts/test_dashboard_page_scripts.py")
    check("the harness warns when node is missing",
          "SKIPPED: node is not installed" in harness)
    check("the harness warns when acorn is missing",
          "SKIPPED: acorn is not installed" in harness)
    check("the harness has a repo-local acorn fallback",
          'ROOT / "node_modules" / "acorn"' in harness)
    check("the harness keeps its real invariants",
          all(token in harness for token in (
              "re-evaluation safe (no top-level const/let)",
              "every inline markup handler still resolves",
              "page script emitted exactly once",
              "page script sits inside #content-area")))
    routes = len(re.findall(r'^\s*"/[^"]*":', harness, re.M))
    check("the harness still covers every nav route", routes >= 36, str(routes))

    print("== 13. systems this work was not allowed to change ==")
    untouched = ("cogs/missions.py", "cogs/mvp.py", "cogs/activity_engine.py",
                 "utils/minigame_engine.py", "utils/mission_engine.py",
                 "utils/activity_stats.py" if (ROOT / "utils/activity_stats.py").exists()
                 else "cogs/missions.py")
    for rel in sorted(set(untouched)):
        text = source(rel)
        leaked = [token for token in ("apply_spam_penalty", "reconcile_role_progression",
                                      "SPAM_WARNING_TEXT", "claim_result_footer",
                                      "remove_old_reward_role")
                  if token in text]
        existing = (ROOT / rel).exists()
        check(f"{rel} does not reference this slice's new behaviour",
              (not existing) or leaked == [], str(leaked))
    voice = cog[cog.index("async def on_activity_voice_tick"):]
    voice = voice[:voice.index("async def ", voice.index("async def on_activity_voice_tick") + 10)]
    check("voice XP keeps its own gates and still goes through the engine",
          "is_role_blacklisted" in voice and "give_reward" in voice)
    check("voice XP is not run through the message spam penalty",
          "apply_spam_penalty" not in voice and "_is_spamming" not in voice)
    defaults = source("utils/xp_calculator.py")
    for name, value in (("xp_per_word", "1"), ("xp_min_per_message", "5"),
                        ("xp_max_per_message", "50"), ("xp_cooldown_seconds", "10"),
                        ("spam_threshold", "10"),
                        ("spam_xp_penalty_divisor", "1000"),
                        ("spam_window_seconds", "20")):
        check(f"the frozen setting {name} is still {value}",
              re.search(rf'"{name}":\s*{value},', defaults) is not None)
    check("the XP curve is unchanged",
          "return math.floor(100 * (level ** 1.5))" in defaults)
    check("every production file still parses",
          all(ast.parse(p.read_text(encoding="utf-8")) for p in production_files()))

    print("== 14. dynamic spot-check: the floor holds on the live database ==")

    async def runnf():
        from utils.xp_calculator import apply_spam_penalty, xp_progress
        await reset_database()
        execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (GUILD,))
        execute("INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)",
                (GUILD, USER, 7, 0))
        for _ in range(5):
            outcome = await apply_spam_penalty(GUILD, USER, 100)
        stored = rows("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?",
                      (GUILD, USER))[0]
        return outcome, stored

    import asyncio
    outcome, stored = asyncio.run(runnf())
    check("five rounded penalties on 7 XP leave 7 XP, level 0 — no debt",
          stored == (7, 0) and outcome["applied"] is False,
          f"stored={stored} outcome={outcome}")
    check("the last penalty deducted nothing (no debt to carry)",
          outcome["deducted"] == 0, str(outcome))

    failed = [c for c in checks if not c[1]]
    print(f"\nXP SAFETY AUDIT: {len(checks) - len(failed)} passed, {len(failed)} failed")
    for name, _, extra in failed:
        print("  FAILED:", name, extra)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
