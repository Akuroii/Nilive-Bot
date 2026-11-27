"""Regression tests: OWNER_ID / DEV_ID tiers, developer-only operator actions, guild allowlist.

Run: python tests/test_owner_dev_tiers.py     (or: python -m pytest -q tests/test_owner_dev_tiers.py)

Anything that depends on import-time env (database.py reads OWNER_ID / DEV_ID at import) runs in a
fresh subprocess with a throwaway SQLite file, so tests cannot leak state into each other.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils import guild_allowlist as gal  # noqa: E402
from utils.owner_ids import OwnerConfigError, load_tiers, parse_id_list  # noqa: E402

DEV = 100000000000000001
OWNER = 100000000000000002
STRANGER = 100000000000000003
FAKE_TOKEN = "A" * 24 + "." + "B" * 6 + "." + "C" * 30


def run_py(code: str, **env_overrides):
    """Run `code` in a fresh interpreter. Returns (returncode, stdout, stderr)."""
    tmp = tempfile.mkdtemp(prefix="nilive-tiers-")
    env = {k: v for k, v in os.environ.items()
           if k not in ("OWNER_ID", "DEV_ID", "ALLOWED_GUILD_IDS", "GUILD_ALLOWLIST_MODE",
                        "SUPER_ADMIN_USER_IDS", "BACKUP_TRUSTED_USER_IDS")}
    env.update(DATABASE_PATH=os.path.join(tmp, "t.db"), SECRET_KEY="x" * 48,
               NERO_ENVIRONMENT="test", DISCORD_TOKEN=FAKE_TOKEN, PYTHONPATH=str(ROOT))
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def last_json(stdout: str):
    return json.loads([ln for ln in stdout.splitlines() if ln.startswith("{")][-1])


class ParseTests(unittest.TestCase):
    def test_single_and_list(self):
        self.assertEqual(parse_id_list("5", "X"), (5,))
        self.assertEqual(parse_id_list(" 5, 6 ,5,, 7", "X"), (5, 6, 7))
        self.assertEqual(parse_id_list("", "X"), ())

    def test_bad_entry_raises_not_skipped(self):
        for bad in ("12abc", "1,x", "-3", "1.5", "١٢٣"):
            with self.assertRaises(OwnerConfigError, msg=bad):
                parse_id_list(bad, "X")

    def test_load_tiers_legacy_owner_only_means_owner_is_also_dev(self):
        self.assertEqual(load_tiers({"OWNER_ID": str(DEV)}), ((DEV,), (DEV,)))

    def test_load_tiers_split(self):
        self.assertEqual(load_tiers({"OWNER_ID": str(OWNER), "DEV_ID": str(DEV)}), ((OWNER,), (DEV,)))

    def test_owner_required(self):
        with self.assertRaises(OwnerConfigError):
            load_tiers({"DEV_ID": str(DEV)})


class DatabaseTierTests(unittest.TestCase):
    CODE = """
        import json, database as d
        print(json.dumps({"owners": list(d.OWNER_DISCORD_IDS), "devs": list(d.DEV_DISCORD_IDS),
                          "trusted": sorted(d.TRUSTED_DISCORD_IDS), "visible": list(d.VISIBLE_OWNER_IDS),
                          "compat": d.OWNER_DISCORD_ID}))
    """

    def test_legacy_deploy_unchanged(self):
        rc, out, err = run_py(self.CODE, OWNER_ID=str(DEV))
        self.assertEqual(rc, 0, err)
        r = last_json(out)
        self.assertEqual(r["devs"], [DEV])
        self.assertEqual(r["visible"], [])      # nobody newly visible in Access
        self.assertEqual(r["compat"], DEV)

    def test_split_owner_and_dev(self):
        rc, out, err = run_py(self.CODE, OWNER_ID=str(OWNER), DEV_ID=str(DEV))
        self.assertEqual(rc, 0, err)
        r = last_json(out)
        self.assertEqual(r["owners"], [OWNER])
        self.assertEqual(r["devs"], [DEV])
        self.assertEqual(sorted(r["trusted"]), sorted([OWNER, DEV]))
        self.assertEqual(r["visible"], [OWNER])  # owner visible, dev hidden

    def test_typo_stops_startup(self):
        for env in ({"OWNER_ID": str(OWNER), "DEV_ID": "70445x"}, {"OWNER_ID": "abc"}, {"OWNER_ID": None}):
            rc, out, _ = run_py(self.CODE, **env)
            self.assertEqual(rc, 1, env)
            self.assertIn("FATAL", out)


class DashboardTrustTests(unittest.TestCase):
    def test_both_trusted_stranger_not(self):
        rc, out, err = run_py("""
            import json
            from dashboard import permissions as p
            print(json.dumps({
              "super": [p.is_trusted_super_admin(i) for i in (%d, %d, %d)],
              "backup": sorted(p._trusted_backup_user_ids()),
            }))
        """ % (OWNER, DEV, STRANGER), OWNER_ID=str(OWNER), DEV_ID=str(DEV))
        self.assertEqual(rc, 0, err)
        r = last_json(out)
        self.assertEqual(r["super"], [True, True, False])
        self.assertEqual(sorted(r["backup"]), sorted([OWNER, DEV]))


class BotTests(unittest.TestCase):
    """Imports the real main.py (no network) and drives the real command objects."""

    def test_owner_visible_in_access_dev_hidden_and_backfill_idempotent(self):
        rc, out, err = run_py("""
            import asyncio, json, sqlite3, types
            import main, database
            from database import init_db, DB_PATH
            asyncio.run(init_db())
            class G(types.SimpleNamespace): pass
            main.bot.__dict__  # real bot object exists
            guilds = [G(id=111, owner_id=222), G(id=333, owner_id=444)]
            # backfill_guild_owners iterates bot.guilds; swap in plain objects for this run only
            type(main.bot).guilds = property(lambda self: guilds)
            asyncio.run(main.backfill_guild_owners())
            asyncio.run(main.backfill_guild_owners())   # second run must not duplicate
            rows = sqlite3.connect(DB_PATH).execute(
                "SELECT guild_id, user_id, permission_level FROM dashboard_users ORDER BY 1,2").fetchall()
            print(json.dumps({"rows": rows}))
        """, OWNER_ID=str(OWNER), DEV_ID=str(DEV))
        self.assertEqual(rc, 0, err)
        rows = [tuple(r) for r in last_json(out)["rows"]]
        self.assertIn((111, OWNER, "owner"), rows)
        self.assertIn((333, OWNER, "owner"), rows)
        self.assertEqual(len(rows), 4)                                    # 2 guild owners + 2 bot-owner rows
        self.assertFalse([r for r in rows if r[1] == DEV], "developer must stay hidden")

    def test_legacy_deploy_writes_no_extra_rows(self):
        rc, out, err = run_py("""
            import asyncio, json, sqlite3, types
            import main
            from database import init_db, DB_PATH
            asyncio.run(init_db())
            guilds = [types.SimpleNamespace(id=111, owner_id=222)]
            type(main.bot).guilds = property(lambda self: guilds)
            asyncio.run(main.backfill_guild_owners())
            rows = sqlite3.connect(DB_PATH).execute("SELECT guild_id, user_id FROM dashboard_users").fetchall()
            print(json.dumps({"rows": rows}))
        """, OWNER_ID=str(DEV))
        self.assertEqual(rc, 0, err)
        self.assertEqual([tuple(r) for r in last_json(out)["rows"]], [(111, 222)])

    def test_is_owner_follows_env_and_sync_reload_are_dev_only(self):
        rc, out, err = run_py("""
            import asyncio, json, types
            import main
            async def go():
                u = lambda i: types.SimpleNamespace(id=i)
                res = {"owner_is_owner": await main.bot.is_owner(u(%(o)d)),
                       "dev_is_owner": await main.bot.is_owner(u(%(d)d)),
                       "stranger_is_owner": await main.bot.is_owner(u(%(s)d))}
                for name in ("sync", "reload"):
                    cmd = main.bot.get_command(name)
                    ctx = lambda i: types.SimpleNamespace(author=u(i))
                    res[name] = [await cmd.can_run(ctx(%(o)d)) if False else None]
                    checks = cmd.checks
                    async def run_checks(i):
                        out = []
                        for c in checks:
                            r = c(ctx(i)); r = await r if asyncio.iscoroutine(r) else r
                            out.append(bool(r))
                        return all(out)
                    res[name] = [await run_checks(%(d)d), await run_checks(%(o)d), await run_checks(%(s)d)]
                return res
            print(json.dumps(asyncio.run(go())))
        """ % {"o": OWNER, "d": DEV, "s": STRANGER}, OWNER_ID=str(OWNER), DEV_ID=str(DEV))
        self.assertEqual(rc, 0, err)
        r = last_json(out)
        self.assertEqual((r["owner_is_owner"], r["dev_is_owner"], r["stranger_is_owner"]), (True, True, False))
        self.assertEqual(r["sync"], [True, False, False])      # dev yes, owner no, stranger no
        self.assertEqual(r["reload"], [True, False, False])


class AllowlistLogicTests(unittest.TestCase):
    def cfg(self, **env):
        return gal.load_allowlist(env)

    def test_empty_list_is_off(self):
        c = self.cfg()
        self.assertEqual(c.mode, gal.OFF)
        self.assertEqual(gal.decide(c, 1), gal.ALLOW)
        self.assertEqual(gal.decide(self.cfg(GUILD_ALLOWLIST_MODE="enforce"), 1), gal.ALLOW)

    def test_default_mode_is_log_never_enforce(self):
        c = self.cfg(ALLOWED_GUILD_IDS="10,20")
        self.assertEqual(c.mode, gal.LOG)
        self.assertEqual(gal.decide(c, 10), gal.ALLOW)
        self.assertEqual(gal.decide(c, 99), gal.LOG_ONLY)

    def test_enforce_leaves_unlisted_only(self):
        c = self.cfg(ALLOWED_GUILD_IDS="10,20", GUILD_ALLOWLIST_MODE="enforce")
        self.assertEqual(gal.decide(c, 20), gal.ALLOW)
        self.assertEqual(gal.decide(c, 99), gal.LEAVE)

    def test_bad_mode_falls_back_to_log_with_a_problem(self):
        c = self.cfg(ALLOWED_GUILD_IDS="10", GUILD_ALLOWLIST_MODE="enforc")
        self.assertEqual(c.mode, gal.LOG)
        self.assertTrue(c.problems)

    def test_typo_in_list_disables_feature(self):
        c = self.cfg(ALLOWED_GUILD_IDS="10,2O", GUILD_ALLOWLIST_MODE="enforce")
        self.assertEqual(c.mode, gal.OFF)
        self.assertEqual(gal.decide(c, 99), gal.ALLOW)
        self.assertTrue(c.problems)

    def test_sweep_leaves_only_unlisted(self):
        c = self.cfg(ALLOWED_GUILD_IDS="10,20", GUILD_ALLOWLIST_MODE="enforce")
        p = gal.sweep_plan(c, [10, 20, 99])
        self.assertEqual((p.leave, p.refused), ((99,), None))

    def test_sweep_refuses_when_nothing_matches(self):
        c = self.cfg(ALLOWED_GUILD_IDS="11,21", GUILD_ALLOWLIST_MODE="enforce")   # typo'd IDs
        p = gal.sweep_plan(c, [10, 20])
        self.assertEqual(p.leave, ())
        self.assertTrue(p.refused)

    def test_log_mode_never_leaves(self):
        c = self.cfg(ALLOWED_GUILD_IDS="10")
        p = gal.sweep_plan(c, [10, 99])
        self.assertEqual((p.leave, p.log_only), ((), (99,)))


class AllowlistRuntimeTests(unittest.TestCase):
    def test_join_and_sweep_call_leave_only_when_enforcing(self):
        rc, out, err = run_py("""
            import asyncio, json, types
            from utils import guild_allowlist as gal
            class G:
                def __init__(s, i): s.id=i; s.name="g%d"%i; s.left=False
                async def leave(s): s.left=True
            bot = types.SimpleNamespace(guilds=[G(10), G(99)])
            bot.get_guild = lambda i: next((g for g in bot.guilds if g.id == i), None)
            async def go():
                a = G(99); kept = await gal.handle_join(a)
                b = G(10); kept_b = await gal.handle_join(b)
                plan = await gal.run_sweep(bot)
                return {"join99_kept": kept, "join99_left": a.left, "join10_kept": kept_b,
                        "join10_left": b.left, "sweep_left": [g.id for g in bot.guilds if g.left]}
            print(json.dumps(asyncio.run(go())))
        """, ALLOWED_GUILD_IDS="10", GUILD_ALLOWLIST_MODE="enforce")
        self.assertEqual(rc, 0, err)
        r = last_json(out)
        self.assertEqual((r["join99_kept"], r["join99_left"]), (False, True))
        self.assertEqual((r["join10_kept"], r["join10_left"]), (True, False))
        self.assertEqual(r["sweep_left"], [99])

        rc, out, err = run_py(out and """
            import asyncio, json, types
            from utils import guild_allowlist as gal
            class G:
                def __init__(s, i): s.id=i; s.name="g"; s.left=False
                async def leave(s): s.left=True
            bot = types.SimpleNamespace(guilds=[G(10), G(99)])
            bot.get_guild = lambda i: next((g for g in bot.guilds if g.id == i), None)
            a = G(99)
            kept = asyncio.run(gal.handle_join(a)); asyncio.run(gal.run_sweep(bot))
            print(json.dumps({"kept": kept, "left": a.left, "sweep": [g.left for g in bot.guilds]}))
        """, ALLOWED_GUILD_IDS="10")  # default mode = log
        self.assertEqual(rc, 0, err)
        r = last_json(out)
        self.assertEqual((r["kept"], r["left"], r["sweep"]), (True, False, [False, False]))


class LeaveCommandTests(unittest.TestCase):
    CODE = """
        import asyncio, json, types
        import discord
        from cogs.guild_admin import GuildAdmin
        class G:
            def __init__(s, i): s.id=i; s.name="Gx"; s.member_count=5; s.owner_id=1; s.left=False
            async def leave(s): s.left=True
        guilds = {10: G(10), 99: G(99)}
        bot = types.SimpleNamespace(get_guild=lambda i: guilds.get(i))
        cog = GuildAdmin(bot)
        class Resp:
            def __init__(s): s.sent=[]
            async def send_message(s, content=None, **kw): s.sent.append((content, kw.get("view") is not None))
        def mk(uid):
            i = types.SimpleNamespace(user=types.SimpleNamespace(id=uid), response=Resp())
            async def orig(): return None
            i.original_response = orig
            return i
        async def go():
            res = {}
            for label, uid, gid in (("stranger", %(s)d, "99"), ("owner", %(o)d, "99"), ("dev_bad", %(d)d, "9x"),
                                    ("dev_protected", %(d)d, "10"), ("dev_unknown", %(d)d, "12345"),
                                    ("dev_ok", %(d)d, "99")):
                i = mk(uid)
                await GuildAdmin.dev_leave_guild.callback(cog, i, gid)
                res[label] = i.response.sent[0]
            res["left_after_command"] = guilds[99].left     # command alone must not leave: needs the button
            return res
        print(json.dumps(asyncio.run(go())))
    """

    def test_command_paths(self):
        rc, out, err = run_py(self.CODE % {"s": STRANGER, "o": OWNER, "d": DEV},
                              OWNER_ID=str(OWNER), DEV_ID=str(DEV), ALLOWED_GUILD_IDS="10")
        self.assertEqual(rc, 0, err)
        r = last_json(out)
        self.assertEqual(r["stranger"][0], "Developer only.")
        self.assertEqual(r["owner"][0], "Developer only.")                 # bot owner is NOT a developer
        self.assertIn("not a server ID", r["dev_bad"][0])
        self.assertIn("protected", r["dev_protected"][0])
        self.assertIn("not in a server", r["dev_unknown"][0])
        self.assertTrue(r["dev_ok"][1], "confirmation view must be attached")
        self.assertIn("Leave", r["dev_ok"][0])
        self.assertFalse(r["left_after_command"], "leaving must wait for the confirm button")

    def test_confirm_and_cancel_buttons(self):
        rc, out, err = run_py("""
            import asyncio, json, types
            from cogs.guild_admin import _ConfirmLeave
            class G:
                id=99; name="Gx"; left=False
                async def leave(s): s.left=True
            class Resp:
                def __init__(s): s.edits=[]
                async def edit_message(s, **kw): s.edits.append(kw.get("content"))
                async def send_message(s, content=None, **kw): s.edits.append(content)
            def inter(uid): return types.SimpleNamespace(user=types.SimpleNamespace(id=uid), response=Resp())
            async def go():
                g = G(); v = _ConfirmLeave(%(d)d, g)
                other = inter(%(s)d); allowed_other = await v.interaction_check(other)
                mine = inter(%(d)d);  allowed_me = await v.interaction_check(mine)
                await v.confirm.callback(mine)
                g2 = G(); v2 = _ConfirmLeave(%(d)d, g2); c = inter(%(d)d)
                await v2.cancel.callback(c)
                return {"stranger_blocked": not allowed_other, "me_allowed": allowed_me,
                        "left_on_confirm": g.left, "left_on_cancel": g2.left, "cancel_msg": c.response.edits[0]}
            print(json.dumps(asyncio.run(go())))
        """ % {"d": DEV, "s": STRANGER}, OWNER_ID=str(OWNER), DEV_ID=str(DEV))
        self.assertEqual(rc, 0, err)
        r = last_json(out)
        self.assertEqual((r["stranger_blocked"], r["me_allowed"]), (True, True))
        self.assertEqual((r["left_on_confirm"], r["left_on_cancel"]), (True, False))
        self.assertIn("stays", r["cancel_msg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
