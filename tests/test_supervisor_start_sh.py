"""Subprocess tests for the REAL start.sh (the Docker CMD) and the REAL utils.startup_barrier.

start.sh runs in a sandbox APP_DIR: `utils/` is a symlink to the repository's utils (real barrier,
real db_config); main.py and dashboard/app.py are stub services. The stub bot keeps re-asserting
guilds_synced='1' plus a fresh lease (like a live bot restoring authorization) until it is killed,
so these tests prove the persisted state is invalidated only after every service is dead.
Run: python tests/test_supervisor_start_sh.py
"""
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
START_SH = REPO / "start.sh"

STUB_BOT = r'''
import os, sqlite3, subprocess, sys, time
d = os.environ["STUB_DIR"]
db = os.environ["DATABASE_PATH"]

def state():
    c = sqlite3.connect(db, timeout=5)
    try:
        return dict(c.execute("SELECT key, value FROM bot_sync_state").fetchall())
    finally:
        c.close()

n = len([f for f in os.listdir(d) if f.startswith("bot") and f.endswith(".pid")]) + 1
open(d + f"/launch{n}.state", "w").write(repr(state()))        # DB as seen at launch
open(d + f"/bot{n}.pid", "w").write(str(os.getpid()))
child = subprocess.Popen(["sleep", "300"])                      # grandchild in the same group
open(d + f"/child{n}.pid", "w").write(str(child.pid))

mode = os.environ.get("STUB_BOT_MODE", "serve")
if mode == "crash_first" and n == 1:
    c = sqlite3.connect(db, timeout=5)
    c.execute("INSERT OR REPLACE INTO bot_sync_state VALUES ('guilds_synced','1','t')")
    c.execute("INSERT OR REPLACE INTO bot_sync_state VALUES ('bot_sync_lease','1','2099-01-01T00:00:00+00:00')")
    c.commit(); c.close()
    sys.exit(7)
if mode == "break_db_then_crash":
    os.rename(db, db + ".bak"); os.mkdir(db)                    # makes the next barrier fail
    sys.exit(9)

while True:                                                     # a live bot re-asserting authorization
    try:
        c = sqlite3.connect(db, timeout=5)
        c.execute("INSERT OR REPLACE INTO bot_sync_state VALUES ('guilds_synced','1','t')")
        c.execute("INSERT OR REPLACE INTO bot_sync_state VALUES ('bot_sync_lease','1','2099-01-01T00:00:00+00:00')")
        c.commit(); c.close()
    except sqlite3.Error:
        pass
    time.sleep(0.02)
'''

STUB_DASHBOARD = r'''
import os, sys, time
d = os.environ["STUB_DIR"]
open(d + "/dash.pid", "w").write(str(os.getpid()))
code = os.environ.get("STUB_DASH_EXIT")
if code is not None:
    while not os.path.exists(d + "/bot1.pid"):
        time.sleep(0.02)
    time.sleep(0.3)
    sys.exit(int(code))
time.sleep(300)
'''


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (FileNotFoundError, ProcessLookupError):
        return False


def _wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nilive-sup-"))
        self.app = self.tmp / "app"
        (self.app / "dashboard").mkdir(parents=True)
        os.symlink(REPO / "utils", self.app / "utils")
        (self.app / "main.py").write_text(STUB_BOT)
        (self.app / "dashboard" / "app.py").write_text(STUB_DASHBOARD)
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        self.db = str(self.tmp / "data" / "nero.db")
        self.proc = None

    def tearDown(self):
        if getattr(self, "log", None):
            self.log.close()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for pid_file in self.stub.glob("*.pid"):
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **extra_env):
        env = dict(os.environ, APP_DIR=str(self.app), STUB_DIR=str(self.stub), DATABASE_PATH=self.db,
                   OWNER_ID="1", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(self.app))
        env.pop("NERO_ENVIRONMENT", None)
        env.update(extra_env)
        self.log = open(self.tmp / "start.log", "w")
        self.proc = subprocess.Popen(["bash", str(START_SH)], cwd=str(self.tmp), env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)

    def output(self) -> str:
        self.log.flush()
        return (self.tmp / "start.log").read_text()

    def pids(self):
        return [int(p.read_text()) for p in sorted(self.stub.glob("*.pid"))]

    def db_state(self):
        c = sqlite3.connect(self.db, timeout=5)
        try:
            return dict(c.execute("SELECT key, value FROM bot_sync_state").fetchall())
        finally:
            c.close()

    def wait_services_up(self):
        self.assertTrue(_wait_for(lambda: (self.stub / "bot1.pid").exists() and (self.stub / "dash.pid").exists()),
                        "services did not start:\n" + self.output())
        time.sleep(0.5)  # let the stub bot re-assert authorization a few times

    def assert_all_dead_and_invalidated(self):
        for pid in self.pids():
            self.assertFalse(_alive(pid), f"pid {pid} still alive after container exit:\n{self.output()}")
        state = self.db_state()
        self.assertEqual(state.get("guilds_synced"), "0", f"persisted authorization not invalidated: {state}")
        self.assertNotIn("bot_sync_lease", state, f"lease not purged: {state}")

    def test_01_initial_barrier_runs_before_any_service(self):
        self.start()
        self.wait_services_up()
        self.assertEqual(ast_first_state(self.stub / "launch1.state"), {"guilds_synced": "0"},
                         "bot launched against a database that was not invalidated first")
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(timeout=20)

    def test_02_sigterm_reaps_all_groups_then_invalidates(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                self.tearDown(); self.setUp()
                self.start()
                self.wait_services_up()
                self.assertEqual(len(self.pids()), 3, "expected bot, bot grandchild and dashboard pids")
                self.proc.send_signal(sig)
                self.assertEqual(self.proc.wait(timeout=20), 0, self.output())
                self.assert_all_dead_and_invalidated()

    def test_03_dashboard_exit_takes_container_down(self):
        self.start(STUB_DASH_EXIT="3")
        self.assertEqual(self.proc.wait(timeout=30), 3, self.output())
        self.assert_all_dead_and_invalidated()

    def test_04_initial_barrier_failure_aborts_before_services(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a regular file, so the database directory cannot be created")
        self.start(DATABASE_PATH=str(blocker / "nero.db"))
        self.assertEqual(self.proc.wait(timeout=20), 1, self.output())
        self.assertEqual(list(self.stub.iterdir()), [], "a service started despite the failed barrier")

    def test_05_bot_crash_runs_post_exit_and_pre_launch_barriers(self):
        self.start(STUB_BOT_MODE="crash_first")
        self.assertTrue(_wait_for(lambda: (self.stub / "launch2.state").exists(), timeout=30),
                        "bot was not restarted:\n" + self.output())
        self.assertEqual(ast_first_state(self.stub / "launch2.state"), {"guilds_synced": "0"},
                         "restarted bot saw the crashed bot's authorization (post-exit/pre-launch barrier missing)")
        out = self.output()
        self.assertIn("Running post-exit barrier", out)
        self.assertGreaterEqual(out.count("Running pre-launch barrier"), 2)
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=20), 0, out)
        self.assert_all_dead_and_invalidated()

    def test_06_supervisor_barrier_failure_exits_container(self):
        self.start(STUB_BOT_MODE="break_db_then_crash")
        self.assertEqual(self.proc.wait(timeout=30), 1, self.output())
        self.assertIn("Post-exit barrier failed", self.output())
        for pid in self.pids():
            self.assertFalse(_alive(pid), f"pid {pid} survived the container exit")


def ast_first_state(path: Path) -> dict:
    import ast
    return ast.literal_eval(path.read_text())


if __name__ == "__main__":
    unittest.main()
