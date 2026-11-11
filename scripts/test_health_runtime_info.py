"""The /health page reports the runtime SQLite version (read-only).

Why this exists: whether a deployment is exposed to a known SQLite defect (for
example the WAL-reset bug, present through 3.51.2 and fixed in 3.51.3) depends
on the `libsqlite3` the running process linked against - not on the version in a
developer's sandbox and not on `requirements.txt`, which pins no SQLite. The
dashboard process runs in the same image as the bot, so showing
`sqlite3.sqlite_version` there is the smallest way to read the deployed value
without touching production data or shelling into the host.

This test asserts only that the page renders that value. It says nothing about
which version is deployed - that is what reading the page answers.

Run:  python3 scripts/test_health_runtime_info.py
"""

import asyncio
import sqlite3
import unittest
from datetime import datetime, timezone

from phase1_support import GUILD, USER, execute, reset_database

import dashboard.app as dashboard

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)


class HealthRuntimeInfoTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(reset_database())
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        # The runtime table only renders once the bot has reported in, so seed
        # the row the heartbeat cog would write.
        execute("INSERT INTO bot_status (id,started_at,last_heartbeat,loaded_cogs,"
                "failed_cogs,discord_py_version,python_version) "
                "VALUES (1,?,?,'[]','[]','2.7.1','3.11.2')",
                (datetime.now(timezone.utc).isoformat(),
                 datetime.now(timezone.utc).isoformat()))
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "runtime info",
                                 "avatar": None},
                           guild_id=GUILD, expires_at=10 ** 12,
                           csrf_token="runtime-info-csrf")

    def test_health_page_reports_the_running_sqlite_version(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("SQLite (dashboard)", body)
        self.assertIn(sqlite3.sqlite_version, body,
                      "the page must show the version this process actually uses")

    def test_health_page_is_still_html_and_unchanged_otherwise(self):
        response = self.client.get("/health")
        self.assertIn("text/html", response.content_type)
        body = response.get_data(as_text=True)
        # The pre-existing runtime rows must still be there.
        self.assertIn("Python", body)
        self.assertIn("discord.py", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
