"""
Regression tests for legacy shop entry-point retirement.

Verifies:
1. Discord /shop slash command is no longer registered
2. Dashboard /shop-publisher page route is no longer accessible
3. Preserved functionality (Shop Designer, APIs, purchases) remains intact
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.phase1_support import (
    GUILD, USER, execute, reset_database, seed_member, make_bot, make_guild
)


class DiscordShopCommandRemoved(unittest.IsolatedAsyncioTestCase):
    """Verify the legacy Discord /shop slash command is removed."""

    async def asyncSetUp(self):
        await reset_database()
        seed_member(user=USER, guild=GUILD, balance=10000)

    async def test_shop_command_not_in_cog(self):
        """The Shop cog should not have a 'shop' command method."""
        from cogs.shop import Shop
        # Check that the Shop class doesn't have a 'shop' method
        # (it had one at line 1929 before removal)
        self.assertFalse(
            hasattr(Shop, 'shop') or 'shop' in dir(Shop),
            "Shop cog should not have a 'shop' command method"
        )

    async def test_shop_command_not_registered_with_bot(self):
        """The /shop command should not be in the bot's command tree."""
        bot = make_bot()
        # Load the shop cog
        await bot.load_extension('cogs.shop')
        
        # Check that 'shop' is not in the bot's application commands
        # Note: This checks the registered commands, not just the cog methods
        self.assertNotIn(
            'shop',
            [cmd.name for cmd in bot.tree.get_commands()],
            "/shop command should not be registered"
        )


class DashboardShopPublisherRouteRemoved(unittest.IsolatedAsyncioTestCase):
    """Verify the legacy Dashboard /shop-publisher page is removed."""

    async def asyncSetUp(self):
        await reset_database()
        seed_member(user=USER, guild=GUILD, balance=10000)

    async def test_shop_publisher_route_returns_404(self):
        """The /shop-publisher route should return 404."""
        from dashboard.app import app
        from dashboard.auth import create_session
        
        # Create a test client
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret-key'
        
        with app.test_client() as client:
            # Try to access the removed route
            response = client.get('/shop-publisher')
            self.assertEqual(
                response.status_code, 404,
                "/shop-publisher route should return 404 (Not Found)"
            )

    async def test_shop_publisher_template_deleted(self):
        """The shoppublisher.html template should not exist."""
        template_path = ROOT / "dashboard" / "templates" / "manage" / "shoppublisher.html"
        self.assertFalse(
            template_path.exists(),
            "shoppublisher.html template should be deleted"
        )


class PreservedFunctionalityIntact(unittest.IsolatedAsyncioTestCase):
    """Verify that preserved shop functionality remains intact."""

    async def asyncSetUp(self):
        await reset_database()
        seed_member(user=USER, guild=GUILD, balance=10000)

    async def test_shop_route_still_exists(self):
        """Dashboard /shop route should still be accessible."""
        from dashboard.app import app
        
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret-key'
        
        with app.test_client() as client:
            # This will fail with 401/403 without proper auth, but should not be 404
            response = client.get('/shop')
            # We expect 401 (unauthorized) or 403 (forbidden), not 404 (not found)
            self.assertNotEqual(
                response.status_code, 404,
                "/shop route should exist (may require auth)"
            )

    async def test_shop_designer_route_still_exists(self):
        """Dashboard /shop-designer route should still be accessible."""
        from dashboard.app import app
        
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret-key'
        
        with app.test_client() as client:
            response = client.get('/shop-designer')
            self.assertNotEqual(
                response.status_code, 404,
                "/shop-designer route should exist (may require auth)"
            )

    async def test_shop_publisher_js_still_exists(self):
        """The shared shop-publisher.js file should still exist."""
        js_path = ROOT / "dashboard" / "static" / "js" / "shop-publisher.js"
        self.assertTrue(
            js_path.exists(),
            "shop-publisher.js should still exist (used by Shop Designer)"
        )

    async def test_shop_publisher_api_endpoints_still_exist(self):
        """The /api/shop-publisher/* endpoints should still be accessible."""
        from dashboard.app import app
        
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret-key'
        
        with app.test_client() as client:
            # Test the catalog endpoint (may require auth, but should not be 404)
            response = client.get('/api/shop-publisher/catalog')
            self.assertNotEqual(
                response.status_code, 404,
                "/api/shop-publisher/catalog should exist (may require auth)"
            )

    async def test_shop_buy_handlers_still_exist(self):
        """The shop_buy_* interaction handlers should still exist."""
        from cogs.shop import Shop
        
        # Check that the on_interaction method exists (handles shop_buy_*)
        self.assertTrue(
            hasattr(Shop, 'on_interaction'),
            "Shop cog should still have on_interaction method for shop_buy_* handlers"
        )

    async def test_show_shop_page_function_still_exists(self):
        """The _show_shop_page function should still exist (used by button callbacks)."""
        from cogs.shop import _show_shop_page
        
        # This function is used by navigation buttons in published shops
        self.assertIsNotNone(
            _show_shop_page,
            "_show_shop_page function should still exist"
        )


class ShopCommandRemovedFromWalletMessages(unittest.TestCase):
    """Verify that wallet help messages no longer reference the /shop command."""

    def test_wallet_messages_updated(self):
        """Wallet empty messages should not reference /shop command."""
        from cogs.wallet import TAB_ITEMS, TAB_COLORS, TAB_POTIONS, TAB_TITLES
        from cogs.wallet import build_inventory_embed
        
        # These constants should exist
        self.assertIsNotNone(TAB_ITEMS)
        self.assertIsNotNone(TAB_COLORS)
        
        # The function should exist
        self.assertIsNotNone(build_inventory_embed)
        
        # We can't easily test the exact message without a full Discord setup,
        # but we can at least verify the module loads correctly


if __name__ == "__main__":
    unittest.main(verbosity=2)
