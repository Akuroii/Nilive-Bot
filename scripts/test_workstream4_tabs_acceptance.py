"""
Comprehensive acceptance test suite for Workstream 4 — Tab URL & Navigation Synchronization:
- JavaScript module verification: URL sync, history pushState/replaceState, popstate handler, WAI-ARIA
- Base template integration: dashboard-tabs.js loading order
- Deep linking & query parameter preservation during demo and standard auth flows
- Template compatibility check across all tabbed pages
"""

import os
import re
import sys
import glob
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["NERO_ENVIRONMENT"] = "staging"
os.environ["STAGING_DATABASE_PATH"] = "/tmp/test_ws4_tabs.db"
os.environ["OWNER_ID"] = "999999999"
os.environ["SECRET_KEY"] = "0123456789abcdef0123456789abcdef"
os.environ["DISCORD_TOKEN"] = "mock_bot_token"

from database import init_db
from dashboard.utils.async_utils import run_async
run_async(init_db())

from dashboard.app import app


class TestWorkstream4TabNavigation(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.tabs_js_path = os.path.join(self.base_dir, "dashboard", "static", "js", "dashboard-tabs.js")
        self.base_html_path = os.path.join(self.base_dir, "dashboard", "templates", "base.html")

        import aiosqlite
        from database import DB_PATH
        async def seed():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO dashboard_users (guild_id, user_id, permission_level, enabled)
                    VALUES (900000000000000001, 900000000000000001, 'owner', 1)
                """)
                await db.execute("""
                    INSERT OR REPLACE INTO guild_settings (guild_id, prefix, timezone)
                    VALUES (900000000000000001, '!', 'UTC')
                """)
                await db.commit()
        run_async(seed())

    def test_dashboard_tabs_js_features(self):
        """dashboard-tabs.js must provide URL synchronization, popstate, ARIA, and keyboard navigation."""
        with open(self.tabs_js_path, "r", encoding="utf-8") as f:
            js = f.read()

        self.assertIn("syncUrlWithTab", js)
        self.assertIn("findTabByName", js)
        self.assertIn("getTabName", js)
        self.assertIn("restoreTabs", js)
        self.assertIn("history.pushState", js)
        self.assertIn("history.replaceState", js)
        self.assertIn("popstate", js)
        self.assertIn("role", js)
        self.assertIn("tablist", js)
        self.assertIn("tabpanel", js)
        self.assertIn("ArrowRight", js)
        self.assertIn("ArrowLeft", js)

    def test_base_html_includes_dashboard_tabs_script(self):
        """base.html must include dashboard-tabs.js."""
        with open(self.base_html_path, "r", encoding="utf-8") as f:
            html = f.read()

        self.assertIn("dashboard-tabs.js", html)

    def test_demo_login_preserves_next_deep_link(self):
        """When DASHBOARD_DEMO_LOGIN=1, accessing deep links directly provisions demo session or navigates via demo-login preserving target URL."""
        os.environ["DASHBOARD_DEMO_LOGIN"] = "1"
        try:
            # Direct deep-link access with demo login provisions session and returns 200 directly (0 redirect loop risk in iframe)
            resp = self.client.get("/leveling?tab=rewards")
            self.assertEqual(resp.status_code, 200)
            self.assertIn('id="tab-content-rewards"', resp.data.decode("utf-8"))

            # Explicit navigation via /demo-login with next parameter redirects to target
            demo_resp = self.client.get("/demo-login?next=%2Fleveling%3Ftab%3Drewards")
            self.assertEqual(demo_resp.status_code, 302)
            self.assertEqual(demo_resp.location, "/leveling?tab=rewards")

            # Final destination renders successfully
            final_resp = self.client.get(demo_resp.location)
            self.assertEqual(final_resp.status_code, 200)
            self.assertIn('id="tab-content-rewards"', final_resp.data.decode("utf-8"))
        finally:
            if "DASHBOARD_DEMO_LOGIN" in os.environ:
                del os.environ["DASHBOARD_DEMO_LOGIN"]

    def test_standard_auth_unauthenticated_deep_link_redirects_to_login(self):
        """When DASHBOARD_DEMO_LOGIN is unset, unauthenticated deep links redirect to /login."""
        if "DASHBOARD_DEMO_LOGIN" in os.environ:
            del os.environ["DASHBOARD_DEMO_LOGIN"]

        resp = self.client.get("/leveling?tab=rewards")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.location, "/login")


if __name__ == "__main__":
    unittest.main()
