"""Security regression tests that do not require a live database."""
import os
import subprocess
import sys
import unittest

import config
from app import app


class SecurityTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_secret_key_is_configured_and_strong(self):
        self.assertEqual(app.secret_key, config.config.secret_key)
        self.assertGreaterEqual(len(app.secret_key), 32)

    def test_cookie_hardening(self):
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertGreaterEqual(app.config["PERMANENT_SESSION_LIFETIME"].total_seconds(), 3600)

    def test_login_page_sets_httponly_samesite_cookie(self):
        response = self.client.get("/login")
        cookies = response.headers.getlist("Set-Cookie")
        self.assertTrue(cookies, "login page did not set a session cookie")
        session_cookie = next((c for c in cookies if c.startswith("session=")), cookies[0])
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("SameSite=Lax", session_cookie)

    def test_post_requires_csrf_token(self):
        response = self.client.post("/login", data={"username": "x", "password": "y"})
        self.assertEqual(response.status_code, 400)

    def test_security_headers(self):
        response = self.client.get("/login")
        expected = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "Cache-Control": "no-store, max-age=0, must-revalidate",
        }
        for header, value in expected.items():
            self.assertEqual(response.headers.get(header), value, header)
        csp = response.headers.get("Content-Security-Policy", "")
        for directive in ("default-src 'self'", "script-src 'self'", "object-src 'none'",
                          "base-uri 'self'", "form-action 'self'"):
            self.assertIn(directive, csp)

    def test_production_requires_secret_key(self):
        env = dict(os.environ)
        env.update(FLASK_ENV="production", SECRET_KEY="", DISABLE_LOGIN="0",
                   FLASK_DEBUG="0")
        result = subprocess.run(
            [sys.executable, "-c", "import config"],
            cwd=config.BASE_DIR, env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY must be set in production", result.stderr)

    def test_production_rejects_demo_mode(self):
        env = dict(os.environ)
        env.update(FLASK_ENV="production", DISABLE_LOGIN="1", FLASK_DEBUG="0")
        result = subprocess.run(
            [sys.executable, "-c", "import config"],
            cwd=config.BASE_DIR, env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DISABLE_LOGIN=1 is not allowed", result.stderr)


if __name__ == "__main__":
    unittest.main()
