"""
Comprehensive acceptance test suite for Workstream 3 — Mobile Tables:
- Verification of .table-wrap wrapping across all dashboard templates containing <table>
- CSS styling contract: scroll container, momentum touch scrolling, overscroll containment, mobile density
- WCAG 2.2 Target Size and Reflow styling verification for table action buttons and toolbars
- Route render checks ensuring tables are served inside responsive containers
"""

import os
import re
import sys
import glob
import unittest
import aiosqlite

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["NERO_ENVIRONMENT"] = "staging"
os.environ["STAGING_DATABASE_PATH"] = "/tmp/test_ws3_tables.db"
os.environ["OWNER_ID"] = "999999999"
os.environ["SECRET_KEY"] = "0123456789abcdef0123456789abcdef"
os.environ["DISCORD_TOKEN"] = "mock_bot_token"

from database import init_db, DB_PATH
from dashboard.utils.async_utils import run_async
run_async(init_db())

from dashboard.app import app


class TestWorkstream3MobileTables(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.user_id = 999999999
        self.guild_id = 900000000000000001
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.templates_dir = os.path.join(self.base_dir, "dashboard", "templates")
        self.static_css_path = os.path.join(self.base_dir, "dashboard", "static", "css", "main.css")

        async def seed():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM dashboard_users")
                await db.execute("DELETE FROM guild_settings")
                await db.execute("""
                    INSERT INTO dashboard_users (guild_id, user_id, permission_level, enabled)
                    VALUES (?, ?, 'owner', 1)
                """, (self.guild_id, self.user_id))
                await db.execute("""
                    INSERT INTO guild_settings (guild_id, prefix, timezone)
                    VALUES (?, '!', 'UTC')
                """, (self.guild_id,))
                await db.commit()
        run_async(seed())

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["user"] = {
                "id": str(self.user_id),
                "username": "OwnerUser",
                "avatar": None,
            }
            sess["access_token"] = "mock_access_token"
            sess["expires_at"] = 9999999999
            sess["csrf_token"] = "valid_csrf_token_hex"
            sess["guild_id"] = self.guild_id
            sess["guild_name"] = "Test Server"

    def test_all_html_tables_wrapped_in_table_wrap(self):
        """Every <table> in dashboard templates must be enclosed in a .table-wrap container."""
        unwrapped = []
        for path in glob.glob(os.path.join(self.templates_dir, "**", "*.html"), recursive=True):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            for match in re.finditer(r'<table[\s>]', content):
                pos = match.start()
                preceding = content[max(0, pos - 250):pos]
                if "table-wrap" not in preceding:
                    rel_path = os.path.relpath(path, self.base_dir)
                    line_no = content[:pos].count('\n') + 1
                    unwrapped.append(f"{rel_path}:{line_no}")

        self.assertEqual(
            unwrapped,
            [],
            f"Found tables missing .table-wrap container: {unwrapped}"
        )

    def test_css_table_wrap_touch_and_overscroll_properties(self):
        """main.css must include touch scrolling, containment, and custom scrollbar rules for .table-wrap."""
        with open(self.static_css_path, "r", encoding="utf-8") as f:
            css = f.read()

        self.assertIn(".table-wrap", css)
        self.assertIn("overflow-x: auto", css)
        self.assertIn("-webkit-overflow-scrolling: touch", css)
        self.assertIn("overscroll-behavior-x: contain", css)
        self.assertIn("scrollbar-width: thin", css)

    def test_css_mobile_table_density_and_touch_targets(self):
        """main.css must contain mobile media queries optimizing table padding and touch target sizes."""
        with open(self.static_css_path, "r", encoding="utf-8") as f:
            css = f.read()

        self.assertIn("@media (max-width: 768px)", css)
        self.assertIn("@media (max-width: 480px)", css)
        self.assertIn(".btn-sm", css)
        self.assertIn("table.table th", css)
        self.assertIn("table.table td", css)

    def test_table_routes_render_with_table_wrap(self):
        """Key table-bearing routes must render with .table-wrap containers in their response."""
        self._login()
        routes = [
            "/members",
            "/reports",
            "/audit-log",
            "/economy",
            "/ledger",
            "/shop",
            "/inventory",
            "/trade",
            "/missions",
            "/minigames",
            "/mvp",
            "/tag-partners",
            "/tag-missions",
            "/leveling",
            "/tickets",
            "/moderation",
            "/triggers",
            "/custom-commands",
            "/config/access",
            "/events",
        ]

        for route in routes:
            with self.subTest(route=route):
                resp = self.client.get(route)
                self.assertEqual(resp.status_code, 200, f"Route {route} failed with status {resp.status_code}")
                html = resp.data.decode("utf-8")
                self.assertIn('class="table-wrap"', html, f"Route {route} did not contain a .table-wrap element")


if __name__ == "__main__":
    unittest.main()
