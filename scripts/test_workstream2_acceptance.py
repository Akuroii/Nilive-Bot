"""
Acceptance test suite for Workstream 2 (WS5 & WS6):
  WS5: Command Manager Initial-Load Layout Instability (SSR-synchronized state & head CSS)
  WS6: Dashboard UI Emoji-to-SVG Migration & Exception Preservation
"""

import os
import sys
import re
import unittest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["NERO_ENVIRONMENT"] = "staging"
os.environ["STAGING_DATABASE_PATH"] = "/tmp/test_ws2.db"
os.environ["OWNER_ID"] = "999999999"
os.environ["SECRET_KEY"] = "0123456789abcdef0123456789abcdef"
os.environ["DISCORD_TOKEN"] = "mock_bot_token"

from database import init_db
import asyncio
asyncio.run(init_db())

from dashboard.app import app

class Workstream2AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        cls.client = app.test_client()

    def test_ws5_command_manager_ssr_head_and_cards(self):
        """WS5: Verify command-manager.css in head, SSR category pre-filtering, and initial rail alignment."""
        import time
        with self.client.session_transaction() as sess:
            sess['user'] = {'id': '999999999', 'username': 'Developer'}
            sess['expires_at'] = time.time() + 3600
            sess['guild_id'] = 123456789
            sess['csrf_token'] = 'test-token-csrf'
            sess['servers'] = [{'id': 123456789, 'name': 'Test Server', 'icon': None}]
            sess['selected_guild'] = {'id': 123456789, 'name': 'Test Server', 'icon': None}

        res = self.client.get('/commands')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        soup = BeautifulSoup(html, 'html.parser')

        # 1. Verify CSS is in <head>
        head = soup.find('head')
        self.assertIsNotNone(head, "<head> element missing")
        cm_link = head.find('link', href=re.compile(r'command-manager\.css'))
        self.assertIsNotNone(cm_link, "command-manager.css must be linked in document <head>")

        # 2. Verify initial category determination
        active_rail_btn = soup.find('button', class_=re.compile(r'cmd-category-nav\s+active'))
        self.assertIsNotNone(active_rail_btn, "Active category rail item must exist in SSR output")
        active_cat = active_rail_btn.get('data-category')
        self.assertEqual(active_rail_btn.get('aria-pressed'), 'true')

        # 3. Verify workspace title and category select alignment
        ws_title = soup.find(id='cmd-selection-label')
        self.assertIsNotNone(ws_title)
        if active_cat:
            self.assertEqual(ws_title.get_text().strip(), active_cat)
        cat_select = soup.find(id='cmd-category-filter')
        self.assertIsNotNone(cat_select)
        selected_option = cat_select.find('option', selected=True)
        self.assertIsNotNone(selected_option)
        self.assertEqual(selected_option.get('value'), active_cat)

        # 4. Verify pre-hidden vs visible command category cards
        cards = soup.find_all('div', class_='cmd-category-card')
        self.assertGreater(len(cards), 0, "Command category cards must be rendered in HTML")

        visible_count = 0
        hidden_count = 0
        for card in cards:
            card_cat = card.get('data-category')
            style = card.get('style', '')
            if card_cat == active_cat or not active_cat:
                self.assertNotIn('display:none', style.replace(' ', '').lower(),
                                 f"Card for active category {card_cat} should not be display:none at SSR time")
                visible_count += 1
            else:
                self.assertIn('display:none', style.replace(' ', '').lower(),
                              f"Card for non-active category {card_cat} should be pre-hidden at SSR time")
                hidden_count += 1

        self.assertGreater(visible_count, 0, "At least one card should be visible for active category")
        self.assertGreater(hidden_count, 0, "Non-active category cards must be pre-hidden")

    def test_ws6_svg_sprite_symbols_validity(self):
        """WS6: Verify SVG sprite in base.html has valid ids and clean geometry."""
        base_path = os.path.join(os.path.dirname(__file__), '../dashboard/templates/base.html')
        with open(base_path, 'r', encoding='utf-8') as f:
            base_html = f.read()

        soup = BeautifulSoup(base_html, 'html.parser')
        sprite = soup.find('svg', class_='nero-icon-sprite')
        self.assertIsNotNone(sprite, "Base SVG sprite container missing")

        symbols = sprite.find_all('symbol')
        self.assertGreater(len(symbols), 20, "Sprite must contain comprehensive line icon symbols")

        symbol_ids = {s.get('id') for s in symbols}
        expected_ids = {
            'nero-i-home', 'nero-i-sliders', 'nero-i-terminal', 'nero-i-roles',
            'nero-i-shop', 'nero-i-bag', 'nero-i-trophy', 'nero-i-shield',
            'nero-i-users', 'nero-i-message', 'nero-i-speaker', 'nero-i-sparkles',
            'nero-i-gift', 'nero-i-zap', 'nero-i-clock', 'nero-i-file',
            'nero-i-settings', 'nero-i-plus', 'nero-i-save', 'nero-i-eye',
            'nero-i-target', 'nero-i-box', 'nero-i-database', 'nero-i-pulse'
        }
        for exp in expected_ids:
            self.assertIn(exp, symbol_ids, f"Symbol #{exp} missing from base.html SVG sprite")

    def test_ws6_preserved_exceptions(self):
        """WS6: Verify explicit user exceptions are preserved."""
        # 1. Polar Bear 🐻‍❄️ in Embed Builder
        eb_path = os.path.join(os.path.dirname(__file__), '../dashboard/templates/manage/embedbuilder.html')
        with open(eb_path, 'r', encoding='utf-8') as f:
            eb_content = f.read()

        self.assertIn('🐻‍❄️', eb_content, "Explicit user constraint: Polar bear 🐻‍❄️ in embedbuilder.html MUST be preserved")
        self.assertIn('id="eb-emoji-btn"', eb_content)

    def test_ws6_templates_no_raw_ui_emojis_in_headings(self):
        """WS6: Verify card titles across migrated templates use SVG icons and no decorative dingbat emojis."""
        templates_to_check = [
            'dashboard/templates/config/access.html',
            'dashboard/templates/config/general.html',
            'dashboard/templates/config/welcome.html',
            'dashboard/templates/config/boost.html',
            'dashboard/templates/config/creator.html',
            'dashboard/templates/config/announcements.html',
            'dashboard/templates/config/botprofile.html',
            'dashboard/templates/manage/tickets.html',
            'dashboard/templates/manage/shoppublisher.html',
            'dashboard/templates/manage/shopdesigner.html',
            'dashboard/templates/manage/moderation.html',
            'dashboard/templates/manage/triggers.html',
            'dashboard/templates/manage/reactionroles.html',
            'dashboard/templates/manage/customcommands.html',
            'dashboard/templates/systems/economy.html',
            'dashboard/templates/systems/leveling.html',
            'dashboard/templates/systems/events.html',
            'dashboard/templates/systems/inventory.html',
            'dashboard/templates/systems/missions.html',
            'dashboard/templates/systems/shop.html',
            'dashboard/templates/systems/tagpartners.html',
            'dashboard/templates/systems/trade.html',
            'dashboard/templates/systems/minigames.html',
            'dashboard/templates/systems/minigame_builder.html',
            'dashboard/templates/systems/tagmissions.html',
            'dashboard/templates/systems/mvp.html',
            'dashboard/templates/context_switch.html',
            'dashboard/templates/server_select.html'
        ]

        # Common decorative emojis that used to be in card titles
        forbidden_in_titles = ['⚡', '📋', '🎭', '🔨', '💾', '⚙️', '🎁', '✨', '🚫', '📢', '🔄', '🎚️', '🛒', '🤝', '🎯', '🎒', '🗺️', '🎲', '🗂️', '📜', '🏷️']

        for rel_path in templates_to_check:
            full_path = os.path.join(os.path.dirname(__file__), '..', rel_path)
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()

            soup = BeautifulSoup(content, 'html.parser')
            card_titles = soup.find_all(class_='card-title')
            for title in card_titles:
                text = title.get_text()
                for emoji in forbidden_in_titles:
                    self.assertNotIn(emoji, text, f"Found unmigrated emoji '{emoji}' in card-title in {rel_path}: {text.strip()}")

if __name__ == '__main__':
    unittest.main()
