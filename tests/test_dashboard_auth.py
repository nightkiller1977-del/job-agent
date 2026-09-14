"""
Fail-closed authentication tests for the dashboard.

Before this gate existed, only /api/sync and /api/jobs/unhydrated checked the
shared secret — GET / rendered the review queues AND the stored credential
emails, and POST /api/action let anyone mark jobs applied/archived. Every route
except the app's own probes now requires SYNC_SECRET, and an unset secret is
refused rather than silently reopening the site.

The middleware reads the module-level SYNC_SECRET per request, so tests patch
that attribute rather than reloading the shared module (a reload would swap the
app object other test files already hold).
"""
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

    def test_health_probe_still_works(self):
        # Render / Prometheus probes carry no secret and must keep working.
        with patch.object(dm, "SYNC_SECRET", ""):
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.client.get("/metrics").status_code, 200)


class TestRequiresSecretWhenConfigured(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dm.app)
        self._patched = patch.object(dm, "SYNC_SECRET", "testsecret")
        self._patched.start()
        self.addCleanup(self._patched.stop)

    def test_homepage_without_secret_is_403(self):
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_homepage_with_wrong_secret_is_403(self):
        resp = self.client.get("/", headers={"X-Sync-Secret": "wrong"})
        self.assertEqual(resp.status_code, 403)

    def test_action_without_secret_is_403(self):
        # 403 from the middleware, i.e. before the handler's own validation —
        # proof the gate runs for routes that used to be wide open.
        resp = self.client.post("/api/action", json={"job_id": "j1", "action": "applied"})
        self.assertEqual(resp.status_code, 403)

    def test_secret_in_header_passes_the_gate(self):
        # A bogus action is a 400 from the handler, not a 403 from the gate —
        # proving the request reached application code.
        resp = self.client.post(
            "/api/action", json={"job_id": "j1", "action": "bogus"}, headers=_AUTH
        )
        self.assertEqual(resp.status_code, 400)

    def test_secret_in_query_param_authenticates_a_navigation(self):
        # A browser navigation cannot set a header; it carries ?secret= instead.
        # With no MONGODB_URI the authenticated request reaches the handler, which
        # responds 503 "not configured" — i.e. the gate let it through.
        resp = self.client.get("/?secret=testsecret")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("Dashboard not configured", resp.text)

    def test_query_param_with_wrong_secret_is_403(self):
        self.assertEqual(self.client.get("/?secret=wrong").status_code, 403)

    def test_query_param_is_not_accepted_for_mutations(self):
        # A cross-site link/form could carry ?secret=; mutations must require the
        # header, so a query param on POST must not satisfy the gate.
        resp = self.client.post(
            "/api/action?secret=testsecret", json={"job_id": "j1", "action": "applied"}
        )
        self.assertEqual(resp.status_code, 403)


class TestMiddlewareIsWiredIn(unittest.TestCase):
    """Guard the fix itself so it cannot quietly disappear."""

    def test_app_has_shared_secret_middleware(self):
        names = [m.cls.__name__ for m in dm.app.user_middleware]
        self.assertIn("SharedSecretMiddleware", names)


if __name__ == "__main__":
    unittest.main()
