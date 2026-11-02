"""Rendering regressions for the selectively integrated frontend redesign."""
import time
import unittest
from unittest.mock import patch

from html.parser import HTMLParser
import phase1_support as S
import dashboard.app as dashboard


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def find(self, tag, **attrs):
        return [a for name, a in self.tags if name == tag and all(a.get(k) == v for k, v in attrs.items())]


class DashboardRedesignTests(unittest.TestCase):
    def setUp(self):
        S.execute('DELETE FROM dashboard_users WHERE user_id=?', (S.USER,))
        S.execute("INSERT INTO dashboard_users(guild_id,user_id,permission_level,enabled) VALUES (?,?,?,1)",
                  (S.GUILD, S.USER, 'owner'))
        self.client = dashboard.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user={'id': str(S.USER), 'username': 'Reviewer'}, guild_id=S.GUILD,
                        expires_at=time.time()+3600, csrf_token='test-token', access_token='test-token')

    def test_shell_overview_and_commands_render(self):
        overview = self.client.get('/')
        self.assertEqual(overview.status_code, 200)
        html = overview.get_data(as_text=True)
        tags = Tags(html)
        self.assertIn('overview-hero', html)
        self.assertIn('Bot Status', html)
        self.assertTrue(tags.find('a', href='/commands', **{'hx-get': '/commands'}))
        self.assertEqual(sum('dashboard-tabs.js' in a.get('src', '') for name, a in tags.tags if name == 'script'), 1)
        commands = self.client.get('/commands')
        self.assertEqual(commands.status_code, 200)
        self.assertTrue(Tags(commands.get_data(as_text=True)).find('div', id='command-manager-page'))

    def test_moderation_tabs_and_invalid_fallback(self):
        for tab in ('logs', 'punishments', 'warnings', 'thresholds', 'invalid'):
            response = self.client.get('/moderation', query_string={'tab': tab})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            tags = Tags(html)
            for panel in ('logs', 'punishments', 'warnings', 'thresholds'):
                nodes = tags.find('div', id='tab-content-' + panel)
                self.assertEqual(len(nodes), 1)
                self.assertEqual('display:none' not in nodes[0].get('style', ''),
                                 panel == (tab if tab != 'invalid' else 'logs'))
            self.assertEqual(html.count('// Close edit reason modal on overlay click'), 1)

    def test_picker_keeps_explicit_grants_without_manage_server(self):
        guilds = [{'id': str(S.GUILD), 'name': 'Granted server', 'icon': None, 'permissions': '0'}]
        with patch.object(dashboard, 'fetch_discord_guilds', return_value=guilds):
            response = self.client.get('/server-select')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        tags = Tags(html)
        self.assertTrue(tags.find('a', href='/select-guild/%s' % S.GUILD))
        self.assertIn('Granted server', html)
        self.assertTrue(tags.find('div', id='unified-servers-mount'))
        self.assertTrue(tags.find('script', id='accessible-server-data', type='application/json'))


if __name__ == '__main__':
    unittest.main()
