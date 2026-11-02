"""Server discovery is not authorization; all Discord calls are mocked."""
import unittest
from unittest.mock import patch
import time
import phase1_support as S
from dashboard.app import app
from dashboard import auth

class ServerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user={'id':'5000','username':'member'}, access_token='fake',
                        expires_at=time.time()+3600, csrf_token='fake')

    def test_manage_guild_discovery_does_not_grant_dashboard_access(self):
        guilds = [dict(id='12',name='Manage Only',permissions='32'),
                  dict(id='13',name='Administrator',permissions='8'),
                  dict(id='900719925474099123',name='Large ID',permissions='32'),
                  dict(id='14',name='Member',permissions='0')]
        with patch('dashboard.app.fetch_discord_guilds',return_value=guilds), \
             patch('dashboard.auth.fetch_bot_guilds_full',return_value=[]):
            response=self.client.get('/api/user/servers')
            self.assertEqual(response.status_code,200)
            self.assertEqual({s['id'] for s in response.json['servers']},{12,13,900719925474099123})
            self.assertEqual({s['id_str'] for s in response.json['servers']},{'12','13','900719925474099123'})
            self.assertEqual(self.client.get('/select-guild/12').status_code,403)

    def test_invalid_permission_bitfields_are_not_manageable(self):
        for value in (None, 'invalid', '-1', -1):
            self.assertFalse(auth.guild_permissions_can_manage_server(value))

if __name__=='__main__': unittest.main()
