"""
Fail-closed authentication tests for the dashboard.

Before this gate existed, only /api/sync and /api/jobs/unhydrated checked the
shared secret — GET / rendered the review queues AND the stored credential
emails, and POST /api/action let anyone mark jobs applied/archived. Every route
except the app's own probes and the /login exchange now requires authentication,
and an unset secret is refused rather than silently reopening the site.

Browsers authenticate once at /login, which sets an HttpOnly session cookie; the
shared secret never appears in a URL and is never exposed to page script. Machine
callers keep using the X-Sync-Secret header.

The middleware reads the module-level SYNC_SECRET per request, so tests patch
that attribute rather than reloading the shared module (a reload would swap the
app object other test files already hold).
"""
import secrets
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard.main as dm

_AUTH = {"X-Sync-Secret": "testsecret"}

# SYNC_SECRET is captured into the module at import time, and the middleware reads
# that module attribute. Assign it explicitly here so this file's auth behaviour does
# not depend on which test module imported dashboard.main first.
dm.SYNC_SECRET = "testsecret"


class TestFailClosedWhenSecretUnset(unittest.TestCase):
    """An unset secret must not mean 'auth disabled'."""

    def setUp(self):
        self.client = TestClient(dm.app)

    def test_homepage_is_refused_not_served(self):
        with patch.object(dm, "SYNC_SECRET", ""):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not configured", resp.json()["detail"].lower())

    def test_state_mutating_api_is_refused(self):
        with patch.object(dm, "SYNC_SECRET", ""):
            resp = self.client.post("/api/action", json={"job_id": "j1", "action": "applied"})
        self.assertEqual(resp.status_code, 503)

    def test_login_is_refused_while_unconfigured(self):
        # /login is exempt from the session check, so it must refuse on its own.
        with patch.object(dm, "SYNC_SECRET", ""):
            self.assertEqual(self.client.get("/login").status_code, 503)
            self.assertEqual(self.client.post("/login", data={"secret": "x"}).status_code, 503)

    def test_health_probe_still_works(self):
        # Render's liveness probe carries no secret and must keep working.
        with patch.object(dm, "SYNC_SECRET", ""):
            self.assertEqual(self.client.get("/health").status_code, 200)

    def test_metrics_is_refused_while_unconfigured(self):
        # /metrics is no longer an unauthenticated probe; an unset secret must not
        # silently expose the Prometheus registry.
        with patch.object(dm, "SYNC_SECRET", ""):
            self.assertEqual(self.client.get("/metrics").status_code, 503)


class TestRequiresSecretWhenConfigured(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dm.app)
        self._patched = patch.object(dm, "SYNC_SECRET", "testsecret")
        self._patched.start()
        self.addCleanup(self._patched.stop)

    def test_api_without_secret_is_403(self):
        resp = self.client.post("/api/action", json={"job_id": "j1", "action": "applied"})
        self.assertEqual(resp.status_code, 403)

    def test_api_with_wrong_secret_is_403(self):
        resp = self.client.post(
            "/api/action", json={"job_id": "j1", "action": "applied"},
            headers={"X-Sync-Secret": "wrong"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_machine_header_passes_the_gate(self):
        # A bogus action is a 400 from the handler, not a 403 from the gate —
        # proving the request reached application code.
        resp = self.client.post(
            "/api/action", json={"job_id": "j1", "action": "bogus"}, headers=_AUTH
        )
        self.assertEqual(resp.status_code, 400)

    def test_browser_navigation_redirects_to_login(self):
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_query_param_secret_does_not_authenticate(self):
        # The secret must not be replayable from a URL / browser history / access log.
        resp = self.client.get("/?secret=testsecret", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def test_login_with_wrong_secret_sets_no_cookie(self):
        resp = self.client.post(
            "/login", data={"secret": "wrong", "next": "/"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("ja_session", resp.cookies)

    def test_login_sets_httponly_strict_cookie_then_grants_access(self):
        resp = self.client.post(
            "/login", data={"secret": "testsecret", "next": "/"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/")
        cookie = resp.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)

        # The cookie now satisfies the gate for a navigation and for API calls.
        # No MONGODB_URI in tests, so the handler's own 503 is the success signal.
        page = self.client.get("/")
        self.assertEqual(page.status_code, 503)
        self.assertIn("Dashboard not configured", page.text)

    def test_login_next_cannot_redirect_off_site(self):
        resp = self.client.post(
            "/login",
            data={"secret": "testsecret", "next": "//evil.example.com"},
            follow_redirects=False,
        )
        self.assertEqual(resp.headers["location"], "/")

    def test_tampered_session_cookie_is_rejected(self):
        # A forged signature must not authenticate, even with a well-formed shape.
        self.client.cookies.set("ja_session", "deadbeef.not-a-real-signature")
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)

    def test_session_cookie_invalidated_when_secret_rotates(self):
        self.client.post("/login", data={"secret": "testsecret", "next": "/"}, follow_redirects=False)
        with patch.object(dm, "SYNC_SECRET", "a-different-secret"):
            # Both the old header value and the cookie's signature are tied to the
            # previous secret, so the navigation is denied (redirected to login).
            resp = self.client.get("/", follow_redirects=False)
            self.assertEqual(resp.status_code, 303)
            self.assertEqual(resp.headers["location"], "/login")

    def test_metrics_requires_the_machine_secret(self):
        # The Prometheus registry is not an anonymous route: an unauthenticated GET
        # is denied (redirected to login, not served), and the machine header works.
        resp = self.client.get("/metrics", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")
        self.assertEqual(self.client.get("/metrics", headers=_AUTH).status_code, 200)

    def test_expired_but_correctly_signed_cookie_is_rejected(self):
        # The signature is valid and the secret never rotated, so ONLY server-side
        # expiry can reject this: it is exactly the replay the browser max-age
        # alone used to permit.
        issued_at = int(time.time()) - (dm._SESSION_MAX_AGE + 60)
        payload = f"{secrets.token_urlsafe(32)}.{issued_at}"
        stale = f"{payload}.{dm._session_signature(payload)}"

        self.client.cookies.set("ja_session", stale)
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login")

    def test_future_dated_signed_cookie_is_rejected(self):
        # A signed issue time in the future must not extend the 12h window.
        issued_at = int(time.time()) + 3600
        payload = f"{secrets.token_urlsafe(32)}.{issued_at}"
        future = f"{payload}.{dm._session_signature(payload)}"

        self.client.cookies.set("ja_session", future)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)

    def test_fresh_signed_cookie_still_authenticates(self):
        # Guard against the expiry check over-rejecting: a just-minted token works.
        payload = f"{secrets.token_urlsafe(32)}.{int(time.time())}"
        fresh = f"{payload}.{dm._session_signature(payload)}"

        self.client.cookies.set("ja_session", fresh)
        # No MONGODB_URI in tests, so the handler's own 503 proves the gate passed.
        self.assertEqual(self.client.get("/").status_code, 503)


class TestMiddlewareIsWiredIn(unittest.TestCase):
    """Guard the fix itself so it cannot quietly disappear."""

    def test_app_has_shared_secret_middleware(self):
        names = [m.cls.__name__ for m in dm.app.user_middleware]
        self.assertIn("SharedSecretMiddleware", names)


if __name__ == "__main__":
    unittest.main()
