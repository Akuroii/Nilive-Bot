"""
Regression tests for legacy shop entry-point retirement.

Verifies through static code inspection:
1. Discord /shop slash command is no longer registered
2. Dashboard /shop-publisher page route is no longer accessible
3. Preserved functionality (Shop Designer, APIs, purchases) remains intact

These tests use only file system checks and text parsing.
They do NOT require database connections, Discord tokens, or production credentials.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DiscordShopCommandRemoved(unittest.TestCase):
    """Verify the legacy Discord /shop slash command is removed."""

    def test_shop_command_not_in_source(self):
        """The @app_commands.command(name="shop") should not be in cogs/shop.py."""
        shop_py = ROOT / "cogs" / "shop.py"
        content = shop_py.read_text()
        # Check that the command decorator is removed
        self.assertNotIn(
            '@app_commands.command(name="shop"',
            content,
            "@app_commands.command(name=\"shop\") should be removed from cogs/shop.py"
        )
        # Check that the method definition is removed
        self.assertNotIn(
            'async def shop(self, interaction: discord.Interaction):',
            content,
            "shop() method should be removed from Shop cog"
        )

    def test_on_interaction_still_in_source(self):
        """The on_interaction method should still be in cogs/shop.py."""
        shop_py = ROOT / "cogs" / "shop.py"
        content = shop_py.read_text()
        self.assertIn(
            'async def on_interaction(self, interaction: discord.Interaction):',
            content,
            "on_interaction method should still exist in cogs/shop.py"
        )
        # Verify it handles shop_buy_*
        self.assertIn(
            'shop_buy_',
            content,
            "on_interaction should still handle shop_buy_* custom IDs"
        )

    def test_inventory_command_still_in_source(self):
        """The inventory command should still be in cogs/shop.py."""
        shop_py = ROOT / "cogs" / "shop.py"
        content = shop_py.read_text()
        self.assertIn(
            '@app_commands.command(name="inventory"',
            content,
            "inventory command should still exist in cogs/shop.py"
        )

    def test_show_shop_page_function_still_exists(self):
        """The _show_shop_page function should still be in cogs/shop.py."""
        shop_py = ROOT / "cogs" / "shop.py"
        content = shop_py.read_text()
        self.assertIn(
            'async def _show_shop_page(',
            content,
            "_show_shop_page function should still exist in cogs/shop.py"
        )


class DashboardShopPublisherRouteRemoved(unittest.TestCase):
    """Verify the legacy Dashboard /shop-publisher page is removed."""

    def test_shoppublisher_template_deleted(self):
        """The shoppublisher.html template should not exist."""
        template_path = ROOT / "dashboard" / "templates" / "manage" / "shoppublisher.html"
        self.assertFalse(
            template_path.exists(),
            "shoppublisher.html template should be deleted"
        )

    def test_shop_publisher_route_removed_from_app_py(self):
        """The /shop-publisher route should not be in root app.py."""
        app_py = ROOT / "app.py"
        content = app_py.read_text()
        self.assertNotIn(
            '@app.route("/shop-publisher")',
            content,
            "/shop-publisher route should be removed from app.py"
        )
        self.assertNotIn(
            'def shop_publisher():',
            content,
            "shop_publisher function should be removed from app.py"
        )

    def test_shop_publisher_route_removed_from_dashboard_app_py(self):
        """The /shop-publisher route should not be in dashboard/app.py."""
        dashboard_app_py = ROOT / "dashboard" / "app.py"
        content = dashboard_app_py.read_text()
        self.assertNotIn(
            '@app.route("/shop-publisher")',
            content,
            "/shop-publisher route should be removed from dashboard/app.py"
        )
        self.assertNotIn(
            'def shop_publisher():',
            content,
            "shop_publisher function should be removed from dashboard/app.py"
        )

    def test_navigation_entry_removed(self):
        """The /shop-publisher navigation entry should be removed from base.html."""
        base_html = ROOT / "dashboard" / "templates" / "base.html"
        content = base_html.read_text()
        self.assertNotIn(
            'href="/shop-publisher"',
            content,
            "/shop-publisher navigation link should be removed from base.html"
        )

    def test_palette_entry_removed(self):
        """The p-shop-publisher palette entry should be removed from dashboard.js."""
        dashboard_js = ROOT / "dashboard" / "static" / "js" / "dashboard.js"
        content = dashboard_js.read_text()
        self.assertNotIn(
            "id: 'p-shop-publisher'",
            content,
            "p-shop-publisher palette entry should be removed from dashboard.js"
        )


class DashboardRoutesConfigured(unittest.TestCase):
    """Verify Dashboard route configuration."""

    def test_shop_designer_route_still_in_dashboard_app(self):
        """The /shop-designer route should still be in dashboard/app.py."""
        dashboard_app_py = ROOT / "dashboard" / "app.py"
        content = dashboard_app_py.read_text()
        self.assertIn(
            '@app.route("/shop-designer")',
            content,
            "/shop-designer route should still be in dashboard/app.py"
        )

    def test_shop_route_still_in_dashboard_app(self):
        """The /shop route should still be in dashboard/app.py."""
        dashboard_app_py = ROOT / "dashboard" / "app.py"
        content = dashboard_app_py.read_text()
        self.assertIn(
            '@app.route("/shop")',
            content,
            "/shop route should still be in dashboard/app.py"
        )


class ShopPublisherJSIntact(unittest.TestCase):
    """Verify shared publisher JS is preserved."""

    def test_shop_publisher_js_exists(self):
        """The shop-publisher.js file should still exist."""
        js_path = ROOT / "dashboard" / "static" / "js" / "shop-publisher.js"
        self.assertTrue(
            js_path.exists(),
            "shop-publisher.js should still exist (used by Shop Designer)"
        )

    def test_shop_publisher_preview_exists(self):
        """NERO.shopPublisherPreview should be defined in shop-publisher.js."""
        js_path = ROOT / "dashboard" / "static" / "js" / "shop-publisher.js"
        content = js_path.read_text()
        self.assertIn(
            'NERO.shopPublisherPreview',
            content,
            "NERO.shopPublisherPreview should be defined in shop-publisher.js"
        )


class ShopPublisherCSSIntact(unittest.TestCase):
    """Verify shared publisher CSS is preserved."""

    def test_shop_publisher_css_exists(self):
        """The shop-publisher.css file should still exist."""
        css_path = ROOT / "dashboard" / "static" / "css" / "shop-publisher.css"
        self.assertTrue(
            css_path.exists(),
            "shop-publisher.css should still exist (used by both pages)"
        )


class APIIndexPreserved(unittest.TestCase):
    """Verify API endpoints are preserved."""

    def test_shop_publisher_api_files_exist(self):
        """All shop-publisher API files should still exist."""
        api_files = [
            "shop_categories.py",
            "shop_designs.py",
            "shop_emoji.py",
            "shop_publications.py",
            "shop_publisher.py",
        ]
        for filename in api_files:
            api_path = ROOT / "dashboard" / "api" / filename
            self.assertTrue(
                api_path.exists(),
                f"{filename} should still exist"
            )


class WalletMessagesUpdated(unittest.TestCase):
    """Verify wallet help messages no longer reference /shop command."""

    def test_wallet_messages_do_not_reference_shop_command(self):
        """Wallet help messages should not reference /shop command."""
        wallet_py = ROOT / "cogs" / "wallet.py"
        content = wallet_py.read_text()
        # Check that the old messages with /shop are gone
        self.assertNotIn(
            '`/shop`',
            content,
            "Wallet messages should not reference /shop command"
        )
        # Verify the new messages are present
        self.assertIn(
            'check the server shop',
            content,
            "Wallet messages should use generic 'check the server shop' text"
        )


class ShopDesignerTemplateIntact(unittest.TestCase):
    """Verify Shop Designer template is preserved."""

    def test_shopdesigner_template_exists(self):
        """The shopdesigner.html template should still exist."""
        template_path = ROOT / "dashboard" / "templates" / "manage" / "shopdesigner.html"
        self.assertTrue(
            template_path.exists(),
            "shopdesigner.html template should still exist"
        )

    def test_shopdesigner_uses_shop_publisher_js(self):
        """Shop Designer should still load shop-publisher.js."""
        template_path = ROOT / "dashboard" / "templates" / "manage" / "shopdesigner.html"
        content = template_path.read_text()
        self.assertIn(
            'shop-publisher.js',
            content,
            "shopdesigner.html should still load shop-publisher.js"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
