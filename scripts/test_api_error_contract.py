"""The dashboard's JSON error contract for ``/api/*`` (C4).

The dashboard serves two kinds of clients from ONE Flask app: HTML pages and the
JSON API under ``/api``. Before this contract existed, an unhandled exception or
an unknown path under ``/api`` produced the *rendered HTML* error page (or
Werkzeug's default HTML), so a ``fetch()`` caller that did
``response.json()`` crashed with a parse error instead of seeing the failure.

What this locks down, on the REAL Flask app with real routing, real permissions
and a scratch SQLite database:

  1. an unknown ``/api/...`` path is a JSON 404 (routing failure, the case a
     per-blueprint error handler cannot cover at all);
  2. a wrong HTTP method on a real API route is a JSON 405;
  3. an unhandled exception inside an API view is a JSON 500 that carries a
     generic message — never the exception text, never a traceback;
  4. ordinary HTML pages keep their existing rendered error pages (404 page
     from a real request, 403/500 from their handlers);
  5. every JSON error has the same shape: ``success`` is false, ``code`` is a
     stable machine-readable token, ``error`` is human-readable.

Run:  python3 scripts/test_api_error_contract.py
"""

import unittest
from unittest.mock import patch

from phase1_support import GUILD, USER, execute, reset_database
from werkzeug.exceptions import BadRequest, Forbidden, InternalServerError, NotFound

import dashboard.app as dashboard
import dashboard.api.shop_publisher as publisher_api

app = dashboard.app
app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)

SECRET = "SECRET-INTERNAL-DETAIL-DO-NOT-LEAK"


def _raise_after_close(error):
    """Fail like a real DB/Discord error would: the coroutine is closed, not run."""
    def raiser(coro):
        coro.close()
        raise error
    return raiser


class ApiJsonErrorContractTests(unittest.TestCase):
    def setUp(self):
        self._reset()
        execute("INSERT INTO dashboard_users (guild_id,user_id,permission_level,enabled) "
                "VALUES (?,?,'owner',1)", (GUILD, USER))
        self.client = app.test_client()
        self.login()

    def _reset(self):
        import asyncio
        asyncio.run(reset_database())

    def login(self):
        with self.client.session_transaction() as session:
            session.update(user={"id": USER, "username": "error contract",
                                 "avatar": None},
                           guild_id=GUILD, expires_at=10 ** 12,
                           csrf_token="error-contract-csrf")
        self.headers = {"X-CSRF-Token": "error-contract-csrf"}

    # ── helpers ────────────────────────────────────────────────────────────
    def assert_json_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.get_data(as_text=True)[:400])
        self.assertTrue(response.is_json, "an /api client must never receive HTML")
        payload = response.get_json()
        self.assertIsInstance(payload, dict)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["code"], code)
        self.assertIsInstance(payload["error"], str)
        self.assertTrue(payload["error"])
        return payload

    # ── 1. unknown API path (routing failure) ──────────────────────────────
    def test_unknown_api_path_is_json_not_html(self):
        response = self.client.get("/api/shop-publisher/does-not-exist")
        self.assert_json_error(response, 404, "not_found")
        self.assertNotIn("<html", response.get_data(as_text=True).lower())

    def test_unknown_api_path_is_json_for_every_http_method(self):
        for method in ("get", "post", "put", "delete"):
            response = getattr(self.client, method)(
                "/api/no-such-endpoint", headers=self.headers)
            self.assert_json_error(response, 404, "not_found")

    # ── 2. wrong method on a real API route ───────────────────────────────
    def test_wrong_method_on_a_real_api_route_is_json_405(self):
        response = self.client.post("/api/shop-publisher/catalog", headers=self.headers)
        self.assert_json_error(response, 405, "method_not_allowed")

    def test_json_405_preserves_the_allow_header(self):
        # RFC 9110: a 405 MUST list the supported methods in Allow. Converting
        # the body to JSON must not lose it.
        response = self.client.post("/api/shop-publisher/catalog", headers=self.headers)
        self.assertEqual(response.status_code, 405)
        allow = response.headers.get("Allow")
        self.assertIsNotNone(allow, "the JSON 405 dropped the Allow header")
        methods = {part.strip().upper() for part in allow.split(",")}
        self.assertIn("GET", methods)
        self.assertIn("HEAD", methods)
        self.assertNotIn("POST", methods)

    def test_html_405_keeps_werkzeugs_default_response_and_allow(self):
        # A page route is GET-only: the HTML contract must stay untouched.
        response = self.client.post("/login")
        self.assertEqual(response.status_code, 405)
        self.assertIn("text/html", response.content_type)
        self.assertIsNotNone(response.headers.get("Allow"))
        self.assertIn("GET", response.headers.get("Allow", "").upper())

    # ── 3. unhandled exception inside an API view ─────────────────────────
    def test_unhandled_api_exception_is_a_generic_json_500(self):
        # This route has no local try/except: only the central handler can
        # turn the failure into the API contract (and it must not leak it).
        with patch.object(publisher_api, "run_async",
                          side_effect=_raise_after_close(RuntimeError(SECRET))):
            with self.assertLogs("dashboard.app", level="ERROR") as logs:
                response = self.client.get(
                    "/api/shop-publisher/products/1/options", headers=self.headers)

        payload = self.assert_json_error(response, 500, "internal_error")
        body = response.get_data(as_text=True)
        self.assertNotIn(SECRET, body)
        self.assertNotIn("Traceback", body)
        self.assertNotIn("RuntimeError", body)
        self.assertEqual(payload["error"],
                         "The server could not complete the request.")
        # It was logged rather than silently swallowed.
        self.assertTrue(any(SECRET in line for line in logs.output))

    def test_unhandled_api_exception_does_not_leak_through_error_pages(self):
        with patch.object(publisher_api, "run_async",
                          side_effect=_raise_after_close(ValueError(SECRET))):
            response = self.client.get(
                "/api/shop-publisher/products/1/options", headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("<html", response.get_data(as_text=True).lower())

    # ── 4. HTML pages keep their rendered error pages ─────────────────────
    def test_unknown_page_path_still_renders_the_html_404_page(self):
        response = self.client.get("/economy/no-such-page")
        self.assertEqual(response.status_code, 404)
        self.assertIn("text/html", response.content_type)
        self.assertIn("<html", response.get_data(as_text=True).lower())

    def test_page_403_and_500_handlers_still_render_html(self):
        with app.test_request_context("/economy"):
            forbidden = dashboard.forbidden(Forbidden())
            server_error = dashboard.server_error(InternalServerError())
        self.assertEqual(forbidden[1], 403)
        self.assertIn("html", forbidden[0].lower())
        self.assertEqual(server_error[1], 500)
        self.assertIn("html", server_error[0].lower())

    # ── 5. the same handlers answer JSON under /api ───────────────────────
    def test_handlers_switch_on_the_api_prefix_only(self):
        with app.test_request_context("/api/shop-publisher/catalog"):
            response, status = dashboard.not_found(NotFound())
            self.assertEqual(status, 404)
            self.assertTrue(response.is_json)
        with app.test_request_context("/api/shop-publisher/catalog"):
            response, status = dashboard.forbidden(Forbidden())
            self.assertEqual(status, 403)
            self.assertTrue(response.is_json)
        with app.test_request_context("/api"):
            response, status = dashboard.bad_request(BadRequest())
            self.assertEqual(status, 400)
            self.assertTrue(response.is_json)

    # ── 6. authentication/authorization errors stay JSON as before ────────
    def test_anonymous_api_request_is_json_401(self):
        self.client = app.test_client()
        response = self.client.get("/api/shop-publisher/catalog")
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.is_json)
        self.assertFalse(response.get_json()["success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
