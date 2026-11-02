"""Demo mode must not create a session implicitly in auth decorators."""
import os
import unittest
from unittest.mock import patch

import phase1_support  # routes must use a scratch database
import dashboard.app as dashboard


class DemoAuthenticationTests(unittest.TestCase):
    def test_demo_flag_does_not_authenticate_protected_routes(self):
        with patch.dict(os.environ, {'DASHBOARD_DEMO_LOGIN': '1'}):
            client = dashboard.app.test_client()
            # Exercise login_required, require_page and require_api_permission.
            for path in ('/server-select', '/leveling?tab=rewards'):
                response = client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 302, path)
                self.assertEqual(response.location, '/login')
                with client.session_transaction() as session:
                    self.assertNotIn('user', session)
            response = client.get('/api/leveling/config')
            self.assertEqual(response.status_code, 401)
            with client.session_transaction() as session:
                self.assertNotIn('user', session)

    def test_existing_explicit_demo_login_remains_opt_in(self):
        with patch.dict(os.environ, {'DASHBOARD_DEMO_LOGIN': '0'}):
            self.assertEqual(dashboard.app.test_client().get('/demo-login').status_code, 404)
        with patch.dict(os.environ, {'DASHBOARD_DEMO_LOGIN': '1'}):
            client = dashboard.app.test_client()
            self.assertEqual(client.get('/demo-login').status_code, 302)
            with client.session_transaction() as session:
                self.assertIn('user', session)


if __name__ == '__main__':
    unittest.main()
