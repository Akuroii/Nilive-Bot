"""Cross-tab guild changes must require authorization and an explicit CSRF POST."""
import time
import unittest
import phase1_support as S
import dashboard.app as dashboard

class GuildContextTests(unittest.TestCase):
    def setUp(self):
        S.execute('DELETE FROM dashboard_users WHERE user_id=?',(S.USER,))
        S.execute("INSERT INTO dashboard_users(guild_id,user_id,permission_level,enabled) VALUES (?,?,?,1)",(S.GUILD,S.USER,'admin'))
        self.client=dashboard.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user={'id':str(S.USER),'username':'Member'}, guild_id=S.GUILD,
                        expires_at=time.time()+3600, csrf_token='valid-csrf', user_level='admin')

    def test_authorized_cross_guild_get_requires_explicit_switch(self):
        target=S.GUILD+1
        S.execute("INSERT INTO dashboard_users(guild_id,user_id,permission_level,enabled) VALUES (?,?,?,1)",(target,S.USER,'admin'))
        r=self.client.get('/leveling?tab=rewards&g='+str(target))
        self.assertEqual(r.status_code,200)
        self.assertIn(b'Active Server Mismatch',r.data)
        with self.client.session_transaction() as sess: self.assertEqual(sess['guild_id'],S.GUILD)
        self.assertEqual(self.client.post('/select-guild/'+str(target),data={'next':'/leveling?tab=rewards'}).status_code,403)
        r=self.client.post('/select-guild/'+str(target),data={'csrf_token':'valid-csrf','next':'/leveling?tab=rewards'})
        self.assertEqual(r.status_code,302)
        self.assertEqual(r.location,'/leveling?tab=rewards')
        with self.client.session_transaction() as sess: self.assertEqual(sess['guild_id'],target)

    def test_legacy_get_only_confirms_and_post_requires_csrf(self):
        target = S.GUILD + 1
        S.execute("INSERT INTO dashboard_users(guild_id,user_id,permission_level,enabled) VALUES (?,?,?,1)",
                  (target, S.USER, 'admin'))
        for method in ('get', 'head'):
            response = getattr(self.client, method)('/select-guild/' + str(target))
            self.assertEqual(response.status_code, 200)
            with self.client.session_transaction() as sess:
                self.assertEqual(sess['guild_id'], S.GUILD)
                self.assertEqual(sess['user_level'], 'admin')
        response = self.client.get('/select-guild/' + str(target))
        self.assertIn(b'method="POST"', response.data)
        self.assertIn(b'valid-csrf', response.data)
        for token in ('', 'invalid'):
            self.assertEqual(self.client.post('/select-guild/' + str(target),
                                             data={'csrf_token': token}).status_code, 403)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['guild_id'], S.GUILD)
        self.assertEqual(self.client.post('/select-guild/' + str(target),
                                         data={'csrf_token': 'valid-csrf'}).status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['guild_id'], target)

    def test_legacy_confirmation_rechecks_grant_on_post(self):
        target = S.GUILD + 1
        S.execute("INSERT INTO dashboard_users(guild_id,user_id,permission_level,enabled) VALUES (?,?,?,1)",
                  (target, S.USER, 'admin'))
        self.assertEqual(self.client.get('/select-guild/' + str(target)).status_code, 200)
        S.execute('DELETE FROM dashboard_users WHERE guild_id=? AND user_id=?', (target, S.USER))
        self.assertEqual(self.client.post('/select-guild/' + str(target),
                                         data={'csrf_token': 'valid-csrf'}).status_code, 403)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['guild_id'], S.GUILD)

    def test_legacy_switch_requires_authentication(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        for method in ('get', 'head', 'post'):
            response = getattr(self.client, method)('/select-guild/' + str(S.GUILD),
                                                    follow_redirects=False)
            self.assertNotEqual(response.status_code, 200)
            with self.client.session_transaction() as sess:
                self.assertNotIn('guild_id', sess)

    def test_legacy_get_without_active_guild_does_not_select(self):
        with self.client.session_transaction() as sess:
            sess.pop('guild_id')
        response = self.client.get('/select-guild/' + str(S.GUILD))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'method="POST"', response.data)
        with self.client.session_transaction() as sess:
            self.assertNotIn('guild_id', sess)

    def test_unauthorized_and_invalid_guild_never_switch(self):
        for invalid_id in ('0', '9223372036854775808', '9' * 100):
            self.assertEqual(self.client.get('/select-guild/' + invalid_id).status_code, 400)
            self.assertEqual(self.client.post('/select-guild/' + invalid_id,
                                             data={'csrf_token': 'valid-csrf'}).status_code, 400)
        self.assertEqual(self.client.get('/leveling?g=999123').status_code,403)
        self.assertEqual(self.client.get('/leveling?g=invalid').status_code,400)
        self.assertEqual(self.client.get('/leveling?g=' + '9'*100).status_code,400)
        self.assertEqual(self.client.post('/select-guild/999123',data={'csrf_token':'valid-csrf'}).status_code,403)
        with self.client.session_transaction() as sess: self.assertEqual(sess['guild_id'],S.GUILD)

    def test_api_rejects_stale_or_invalid_context_without_changing_session(self):
        r=self.client.get('/api/leveling/config', headers={'X-Guild-ID':str(S.GUILD+1)})
        self.assertEqual(r.status_code,409)
        self.assertEqual(r.json['code'],'GUILD_CONTEXT_MISMATCH')
        r=self.client.get('/api/leveling/config', headers={'X-Guild-ID':'bad'})
        self.assertEqual(r.status_code,400)
        self.assertEqual(r.json['code'],'MALFORMED_GUILD_CONTEXT')
        r=self.client.get('/api/leveling/config', headers={'X-Guild-ID':str(S.GUILD)})
        self.assertEqual(r.status_code,200)
        with self.client.session_transaction() as sess: self.assertEqual(sess['guild_id'],S.GUILD)

    def test_switch_rejects_external_or_browser_normalized_redirect(self):
        for path in ('//evil.example','/\\evil.example','https://evil.example'):
            r=self.client.post('/select-guild/'+str(S.GUILD),data={'csrf_token':'valid-csrf','next':path})
            self.assertEqual(r.status_code,302)
            self.assertEqual(r.location,'/')

if __name__=='__main__': unittest.main()
