"""
"Sign in with Google" as an alternative to typing the sync secret.

Reuses the existing SYNC_SECRET-signed ja_session cookie on success, so every
downstream route stays unaware of which credential the browser presented.
Google is only reachable through the OAuth code-exchange path here — these
tests fake the token/userinfo HTTP calls rather than hitting Google.
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard.main as dm

dm.SYNC_SECRET = "testsecret"
_ALLOWED_EMAIL = "allowed@example.com"


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient: returns canned token+userinfo responses
    without making a real network call."""

    def __init__(self, token_response=None, userinfo_response=None, raise_on_post=False):
        self._token_response = token_response or _FakeResponse({"access_token": "tok"})
        self._userinfo_response = userinfo_response or _FakeResponse(
            {"email": _ALLOWED_EMAIL, "verified_email": True}
        )
        self._raise_on_post = raise_on_post

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        if self._raise_on_post:
            raise RuntimeError("network error")
        return self._token_response

    async def get(self, *args, **kwargs):
        return self._userinfo_response


def _configured():
    """Patches that make Google sign-in appear fully configured."""
    return (
        patch.object(dm, "GOOGLE_CLIENT_ID", "client-id"),
        patch.object(dm, "GOOGLE_CLIENT_SECRET", "client-secret"),
        patch.object(dm, "ALLOWED_GOOGLE_EMAIL", _ALLOWED_EMAIL),
    )


class TestLoginPageAdvertisesGoogleOnlyWhenConfigured(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dm.app)
        self._patched = patch.object(dm, "SYNC_SECRET", "testsecret")
        self._patched.start()
        self.addCleanup(self._patched.stop)

    def test_no_google_button_when_unconfigured(self):
        with patch.object(dm, "GOOGLE_CLIENT_ID", ""):
            resp = self.client.get("/login")
        self.assertNotIn("Sign in with Google", resp.text)

    def test_google_button_shown_when_fully_configured(self):
        patches = _configured()
        for p in patches:
            p.start()
        try:
            resp = self.client.get("/login")
        finally:
            for p in patches:
                p.stop()
        self.assertIn("Sign in with Google", resp.text)
        self.assertIn("/auth/google/start", resp.text)

    def test_missing_allowed_email_still_hides_button(self):
        # client id/secret alone are not enough — an allow-list-less Google
        # login would authenticate ANY Google account.
        with patch.object(dm, "GOOGLE_CLIENT_ID", "client-id"), \
             patch.object(dm, "GOOGLE_CLIENT_SECRET", "client-secret"), \
             patch.object(dm, "ALLOWED_GOOGLE_EMAIL", ""):
            resp = self.client.get("/login")
        self.assertNotIn("Sign in with Google", resp.text)


class TestGoogleStart(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dm.app)
        self._patched = patch.object(dm, "SYNC_SECRET", "testsecret")
        self._patched.start()
        self.addCleanup(self._patched.stop)

    def test_reachable_without_any_session(self):
        # Part of the login flow itself — must not be caught by the auth gate,
        # i.e. never bounced to /login as a protected route would be (303).
        with patch.object(dm, "GOOGLE_CLIENT_ID", ""):
            resp = self.client.get("/auth/google/start", follow_redirects=False)
        self.assertNotEqual(resp.status_code, 303)
        # It renders the login page's own "not configured" error (401, same
        # convention _login_page already uses for "Invalid sync secret").
        self.assertEqual(resp.status_code, 401)

    def test_unconfigured_falls_back_to_login_page(self):
        with patch.object(dm, "GOOGLE_CLIENT_ID", ""):
            resp = self.client.get("/auth/google/start", follow_redirects=False)
        self.assertEqual(resp.status_code, 401)
        self.assertIn("not configured", resp.text.lower())

    def test_configured_redirects_to_google_with_state_cookie(self):
        patches = _configured()
        for p in patches:
            p.start()
        try:
            resp = self.client.get("/auth/google/start?next=/foo", follow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(resp.status_code, 303)
        location = resp.headers["location"]
        self.assertTrue(location.startswith("https://accounts.google.com/"))
        self.assertIn("client_id=client-id", location)
        self.assertIn("redirect_uri=", location)
        self.assertIn("ja_oauth_state", resp.cookies)

    def test_open_redirect_via_next_is_rejected(self):
        patches = _configured()
        for p in patches:
            p.start()
        try:
            resp = self.client.get("/auth/google/start?next=//evil.example.com", follow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        state_cookie = resp.cookies.get("ja_oauth_state")
        self.assertIsNotNone(state_cookie)
        # The unsafe target must have been normalized to "/", not carried through.
        self.assertTrue(state_cookie.endswith(".//evil.example.com") is False)


class TestGoogleCallback(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(dm.app)
        self._patched = patch.object(dm, "SYNC_SECRET", "testsecret")
        self._patched.start()
        self.addCleanup(self._patched.stop)

    def _start(self, next_path="/"):
        patches = _configured()
        for p in patches:
            p.start()
        try:
            resp = self.client.get(f"/auth/google/start?next={next_path}", follow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        location = resp.headers["location"]
        state = dict(part.split("=", 1) for part in location.split("?", 1)[1].split("&"))["state"]
        return state

    def test_missing_state_cookie_fails_closed(self):
        patches = _configured()
        for p in patches:
            p.start()
        try:
            resp = self.client.get("/auth/google/callback?code=abc&state=whatever", follow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(resp.status_code, 401)
        self.assertIn("failed", resp.text.lower())
        self.assertNotIn("ja_session", resp.cookies)

    def test_state_mismatch_is_rejected(self):
        self._start()
        patches = _configured()
        for p in patches:
            p.start()
        try:
            resp = self.client.get(
                "/auth/google/callback?code=abc&state=not-the-real-state", follow_redirects=False
            )
        finally:
            for p in patches:
                p.stop()
        self.assertNotIn("ja_session", resp.cookies)

    def test_successful_exchange_for_allowed_email_issues_session(self):
        state = self._start(next_path="/dashboard-home")
        patches = _configured()
        for p in patches:
            p.start()
        try:
            with patch.object(dm.httpx, "AsyncClient", _FakeAsyncClient()):
                resp = self.client.get(
                    f"/auth/google/callback?code=abc&state={state}", follow_redirects=False
                )
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/dashboard-home")
        self.assertIn("ja_session", resp.cookies)

    def test_issued_session_cookie_is_samesite_lax_not_strict(self):
        # Regression: the callback request arrives via a cross-site top-level
        # navigation (redirected from accounts.google.com). A browser treats
        # the 303 this handler issues as still part of that same navigation,
        # so a SameSite=Strict cookie set here is silently dropped on that one
        # hop — the sync-secret /login path is same-site throughout and can
        # stay Strict, but this one specifically must be Lax or sign-in
        # succeeds server-side and then immediately bounces back to /login.
        state = self._start()
        patches = _configured()
        for p in patches:
            p.start()
        try:
            with patch.object(dm.httpx, "AsyncClient", _FakeAsyncClient()):
                resp = self.client.get(
                    f"/auth/google/callback?code=abc&state={state}", follow_redirects=False
                )
        finally:
            for p in patches:
                p.stop()
        cookie_header = resp.headers["set-cookie"].lower()
        self.assertIn("samesite=lax", cookie_header)
        self.assertNotIn("samesite=strict", cookie_header)

    def test_wrong_google_account_is_rejected(self):
        state = self._start()
        patches = _configured()
        for p in patches:
            p.start()
        fake = _FakeAsyncClient(
            userinfo_response=_FakeResponse({"email": "someone-else@example.com", "verified_email": True})
        )
        try:
            with patch.object(dm.httpx, "AsyncClient", fake):
                resp = self.client.get(
                    f"/auth/google/callback?code=abc&state={state}", follow_redirects=False
                )
        finally:
            for p in patches:
                p.stop()
        self.assertNotIn("ja_session", resp.cookies)
        self.assertIn("not authorized", resp.text.lower())

    def test_unverified_email_is_rejected(self):
        state = self._start()
        patches = _configured()
        for p in patches:
            p.start()
        fake = _FakeAsyncClient(
            userinfo_response=_FakeResponse({"email": _ALLOWED_EMAIL, "verified_email": False})
        )
        try:
            with patch.object(dm.httpx, "AsyncClient", fake):
                resp = self.client.get(
                    f"/auth/google/callback?code=abc&state={state}", follow_redirects=False
                )
        finally:
            for p in patches:
                p.stop()
        self.assertNotIn("ja_session", resp.cookies)

    def test_google_error_param_fails_closed(self):
        state = self._start()
        patches = _configured()
        for p in patches:
            p.start()
        try:
            resp = self.client.get(
                f"/auth/google/callback?state={state}&error=access_denied", follow_redirects=False
            )
        finally:
            for p in patches:
                p.stop()
        self.assertNotIn("ja_session", resp.cookies)

    def test_network_failure_during_exchange_fails_closed(self):
        state = self._start()
        patches = _configured()
        for p in patches:
            p.start()
        try:
            with patch.object(dm.httpx, "AsyncClient", _FakeAsyncClient(raise_on_post=True)):
                resp = self.client.get(
                    f"/auth/google/callback?code=abc&state={state}", follow_redirects=False
                )
        finally:
            for p in patches:
                p.stop()
        self.assertNotIn("ja_session", resp.cookies)

    def test_issued_session_passes_the_shared_secret_middleware(self):
        # The whole point: a Google-issued session must be indistinguishable
        # downstream from a sync-secret-issued one.
        state = self._start()
        patches = _configured()
        for p in patches:
            p.start()
        try:
            with patch.object(dm.httpx, "AsyncClient", _FakeAsyncClient()):
                self.client.get(f"/auth/google/callback?code=abc&state={state}", follow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        # No MONGODB_URI in tests, so the handler's own 503 (not a 303 to /login)
        # proves the gate passed.
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main()
