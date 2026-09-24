"""
Regression tests for two production bugs fixed on 2026-09-11:

  - POST /api/sync silently overwrote an existing job's discovered_at on
    every re-sync (the Mongo upsert set it via $set instead of
    $setOnInsert). Reachable via orchestrator.py's hydrate_external_jobs,
    which re-syncs a job with no discovered_at in the payload at all.

  - POST /api/jobs/external inserted a job with no `score` key. Jinja2's
    Undefined sentinel passes index.html's `{% if job.score is not none %}`
    guard but raises UndefinedError on the following `{% if job.score >=
    80 %}`, crashing the entire dashboard homepage for every visitor.

Both endpoints call get_db() directly with no dependency-injection seam,
so these tests patch dashboard.main.get_db the same way
tests/test_credentials.py does for /api/credentials.

Every request also carries the shared secret: the dashboard is fail-closed
(see tests/test_dashboard_auth.py), so /api/* and / are 403 without it.
"""
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

with patch.dict("os.environ", {"MONGODB_URI": "", "SYNC_SECRET": "testsecret"}):
    from dashboard.main import app

_AUTH = {"X-Sync-Secret": "testsecret"}


def _chainable(rows):
    """A MagicMock standing in for a pymongo Cursor: supports both
    .sort().limit() (the _sort_score_then_date path) and .limit() directly
    (the plain .find().sort().limit() path used for 'applied'), always
    resolving to the same iterable."""
    cursor = MagicMock()
    cursor.limit.return_value = rows
    cursor.sort.return_value.limit.return_value = rows
    return cursor


class TestSyncPreservesDiscoveredAt(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("dashboard.main.get_db")
    def test_resync_without_discovered_at_does_not_touch_it(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        # The hydration re-sync shape: an already-known job_id, no
        # discovered_at in the payload at all.
        resp = self.client.post(
            "/api/sync",
            json=[{"job_id": "existing-1", "status": "discovered", "title": "Re-synced"}],
            headers={"X-Sync-Secret": "testsecret"},
        )
        self.assertEqual(resp.status_code, 200)

        mock_db.jobs.update_one.assert_called_once()
        filt, update = mock_db.jobs.update_one.call_args[0]
        self.assertEqual(filt, {"job_id": "existing-1"})
        self.assertNotIn(
            "discovered_at", update["$set"],
            "discovered_at must be insert-only — putting it in $set overwrites "
            "an existing job's original discovery date on every re-sync",
        )
        self.assertIn("discovered_at", update["$setOnInsert"])

    @patch("dashboard.main.get_db")
    def test_new_job_still_gets_a_discovered_at_on_insert(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        resp = self.client.post(
            "/api/sync",
            json=[{"job_id": "brand-new-1", "status": "discovered", "title": "New"}],
            headers={"X-Sync-Secret": "testsecret"},
        )
        self.assertEqual(resp.status_code, 200)

        _, update = mock_db.jobs.update_one.call_args[0]
        self.assertIn("discovered_at", update["$setOnInsert"])
        self.assertIsNotNone(update["$setOnInsert"]["discovered_at"])


class TestActionIdempotency(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("dashboard.main.get_db")
    def test_action_atomically_records_idempotency_key(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.jobs.find_one_and_update.return_value = {
            "job_id": "job-1",
            "status": "applied",
            "status_revision": 5,
        }
        mock_get_db.return_value = mock_db

        response = self.client.post(
            "/api/action",
            json={
                "job_id": "job-1",
                "action": "applied",
                "idempotency_key": "stable-key-1",
                "expected_status": "approved",
                "expected_revision": 4,
            },
            headers=_AUTH,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["deduplicated"])
        self.assertEqual(response.json()["status_revision"], 5)
        filt, update = mock_db.jobs.find_one_and_update.call_args.args
        operation_key = "applied:stable-key-1"
        self.assertEqual(filt["_action_idempotency_keys"], {"$ne": operation_key})
        self.assertEqual(filt["status"], "approved")
        self.assertEqual(filt["status_revision"], 4)
        self.assertEqual(update["$inc"]["status_revision"], 1)
        self.assertNotIn("$addToSet", update)
        self.assertEqual(
            update["$push"]["_action_idempotency_keys"]["$each"],
            [operation_key],
        )
        self.assertLess(
            update["$push"]["_action_idempotency_keys"]["$slice"],
            0,
        )

    @patch("dashboard.main.get_db")
    def test_action_replay_returns_success_without_reapplying(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.jobs.find_one_and_update.return_value = None
        mock_db.jobs.find_one.return_value = {
            "job_id": "job-1",
            "status": "applied",
            "status_revision": 5,
            "_action_idempotency_keys": ["applied:stable-key-1"],
        }
        mock_get_db.return_value = mock_db

        response = self.client.post(
            "/api/action",
            json={
                "job_id": "job-1",
                "action": "applied",
                "idempotency_key": "stable-key-1",
                "expected_status": "approved",
                "expected_revision": 4,
            },
            headers=_AUTH,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deduplicated"])
        self.assertEqual(response.json()["status_revision"], 5)
        mock_db.jobs.find_one.assert_called_once()

    @patch("dashboard.main.get_db")
    def test_action_replay_conflicts_when_current_status_changed(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.jobs.find_one_and_update.return_value = None
        mock_db.jobs.find_one.return_value = {
            "job_id": "job-1",
            "status": "approved",
            "status_revision": 6,
            "_action_idempotency_keys": ["applied:stable-key-1"],
        }
        mock_get_db.return_value = mock_db

        response = self.client.post(
            "/api/action",
            json={
                "job_id": "job-1",
                "action": "applied",
                "idempotency_key": "stable-key-1",
                "expected_status": "approved",
                "expected_revision": 4,
            },
            headers=_AUTH,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["current_status"], "approved")
        self.assertEqual(response.json()["detail"]["current_revision"], 6)
        self.assertEqual(
            response.json()["detail"]["conflict_kind"],
            "operation_superseded",
        )
        self.assertTrue(response.json()["detail"]["operation_recorded"])

    @patch("dashboard.main.get_db")
    def test_first_keyed_action_uses_expected_status_compare_and_set(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.jobs.find_one_and_update.return_value = None
        mock_db.jobs.find_one.return_value = {
            "job_id": "job-1",
            "status": "skipped",
            "status_revision": 6,
            "_action_idempotency_keys": [],
        }
        mock_get_db.return_value = mock_db

        response = self.client.post(
            "/api/action",
            json={
                "job_id": "job-1",
                "action": "applied",
                "idempotency_key": "stable-key-1",
                "expected_status": "approved",
                "expected_revision": 4,
            },
            headers=_AUTH,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["message"],
            "Current status or revision no longer matches expected state",
        )
        self.assertEqual(response.json()["detail"]["current_status"], "skipped")
        self.assertEqual(response.json()["detail"]["current_revision"], 6)
        self.assertEqual(
            response.json()["detail"]["conflict_kind"],
            "compare_and_set_failed",
        )
        self.assertFalse(response.json()["detail"]["operation_recorded"])
        filt = mock_db.jobs.find_one_and_update.call_args.args[0]
        self.assertEqual(filt["status"], "approved")
        self.assertEqual(filt["status_revision"], 4)

    @patch("dashboard.main.get_db")
    def test_already_targeted_action_still_records_operation_key(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.jobs.find_one_and_update.return_value = None
        mock_db.jobs.find_one.return_value = {
            "job_id": "job-1",
            "status": "applied",
            "status_revision": 4,
            "_action_idempotency_keys": [],
        }
        mock_db.jobs.update_one.return_value.matched_count = 1
        mock_get_db.return_value = mock_db

        response = self.client.post(
            "/api/action",
            json={
                "job_id": "job-1",
                "action": "applied",
                "idempotency_key": "stable-key-1",
                "expected_status": "approved",
                "expected_revision": 4,
            },
            headers=_AUTH,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deduplicated"])
        self.assertEqual(response.json()["status_revision"], 5)
        filt, update = mock_db.jobs.update_one.call_args.args
        self.assertEqual(filt["status"], "applied")
        self.assertEqual(filt["status_revision"], 4)
        self.assertEqual(update["$inc"]["status_revision"], 1)
        self.assertNotIn("$addToSet", update)
        self.assertEqual(
            update["$push"]["_action_idempotency_keys"]["$each"],
            ["applied:stable-key-1"],
        )
        self.assertLess(
            update["$push"]["_action_idempotency_keys"]["$slice"],
            0,
        )

    @patch("dashboard.main.get_db")
    def test_target_noop_losing_record_race_reports_superseded_operation(
        self, mock_get_db
    ):
        mock_db = MagicMock()
        mock_db.jobs.find_one_and_update.return_value = None
        mock_db.jobs.find_one.side_effect = [
            {
                "job_id": "job-1",
                "status": "applied",
                "status_revision": 4,
                "_action_idempotency_keys": [],
            },
            {
                "job_id": "job-1",
                "status": "skipped",
                "status_revision": 6,
                "_action_idempotency_keys": ["applied:stable-key-1"],
            },
        ]
        mock_db.jobs.update_one.return_value.matched_count = 0
        mock_get_db.return_value = mock_db

        response = self.client.post(
            "/api/action",
            json={
                "job_id": "job-1",
                "action": "applied",
                "idempotency_key": "stable-key-1",
                "expected_status": "approved",
                "expected_revision": 4,
            },
            headers=_AUTH,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["conflict_kind"],
            "operation_superseded",
        )
        self.assertTrue(response.json()["detail"]["operation_recorded"])
        self.assertEqual(response.json()["detail"]["current_status"], "skipped")
        self.assertEqual(response.json()["detail"]["current_revision"], 6)

    @patch("dashboard.main.get_db")
    def test_already_targeted_action_rejects_stale_revision(self, mock_get_db):
        """An away/back cycle must not acknowledge an older generation."""
        mock_db = MagicMock()
        mock_db.jobs.find_one_and_update.return_value = None
        mock_db.jobs.find_one.return_value = {
            "job_id": "job-1",
            "status": "applied",
            "status_revision": 7,
            "_action_idempotency_keys": [],
        }
        mock_get_db.return_value = mock_db

        response = self.client.post(
            "/api/action",
            json={
                "job_id": "job-1",
                "action": "applied",
                "idempotency_key": "newer-generation",
                "expected_status": "skipped",
                "expected_revision": 6,
            },
            headers=_AUTH,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["current_status"], "applied")
        self.assertEqual(response.json()["detail"]["current_revision"], 7)
        mock_db.jobs.update_one.assert_not_called()

    @patch("dashboard.main.get_db")
    def test_unkeyed_compare_and_set_miss_conflicts_at_target(self, mock_get_db):
        """A stale expected status must not become a successful target no-op."""
        mock_db = MagicMock()
        mock_db.jobs.find_one_and_update.return_value = None
        mock_db.jobs.find_one.return_value = {
            "job_id": "job-1",
            "status": "applied",
            "status_revision": 8,
        }
        mock_get_db.return_value = mock_db

        response = self.client.post(
            "/api/action",
            json={
                "job_id": "job-1",
                "action": "applied",
                "expected_status": "approved",
            },
            headers=_AUTH,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["current_status"], "applied")
        self.assertEqual(response.json()["detail"]["current_revision"], 8)


class TestExternalJobScorePlaceholder(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("dashboard.main.get_db")
    def test_external_job_is_inserted_with_score_none_not_missing(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.jobs.find_one.return_value = None  # not a duplicate
        mock_get_db.return_value = mock_db

        resp = self.client.post("/api/jobs/external", json={"url": "https://example.com/jobs/123"}, headers=_AUTH)
        self.assertEqual(resp.status_code, 200)

        mock_db.jobs.insert_one.assert_called_once()
        inserted = mock_db.jobs.insert_one.call_args[0][0]
        self.assertIn("score", inserted)
        self.assertIsNone(inserted["score"])

    @patch("dashboard.main.MONGODB_URI", "mongodb://fake-for-test")
    @patch("dashboard.main.get_db")
    def test_homepage_renders_the_inserted_placeholder_without_crashing(self, mock_get_db):
        # End-to-end through the real add_external_job code path: whatever
        # document it actually inserts is what gets fed back into index()'s
        # render, so a future regression that drops `score` again (or any
        # other field the template dereferences unconditionally) fails this
        # test via a real UndefinedError, not a hand-maintained fixture that
        # could silently drift from what the endpoint actually produces.
        #
        # MONGODB_URI is read into a module-level constant at import time
        # (dashboard/main.py:33) rather than looked up per-request, so
        # patching dashboard.main.get_db alone doesn't satisfy index()'s own
        # `if not MONGODB_URI` gate — it has to be patched directly too.
        mock_db = MagicMock()
        mock_db.jobs.find_one.return_value = None
        mock_get_db.return_value = mock_db

        post_resp = self.client.post("/api/jobs/external", json={"url": "https://example.com/jobs/456"}, headers=_AUTH)
        self.assertEqual(post_resp.status_code, 200)
        inserted_job = mock_db.jobs.insert_one.call_args[0][0]

        mock_db.jobs.find.side_effect = lambda filt, *a, **kw: (
            _chainable([inserted_job]) if filt.get("status") == "discovered" else _chainable([])
        )
        mock_db.jobs.aggregate.return_value = []
        mock_db.jobs.count_documents.return_value = 0
        mock_db.sync_events.find.return_value.sort.return_value.limit.return_value = []
        mock_db.sync_events.find_one.return_value = None
        mock_db.credentials.find.return_value = []

        get_resp = self.client.get("/", headers=_AUTH)
        self.assertEqual(
            get_resp.status_code, 200,
            f"homepage crashed rendering a job with score=None: {get_resp.text[:500]}",
        )
        self.assertIn(inserted_job["job_id"], get_resp.text)

    @patch("dashboard.main.MONGODB_URI", "mongodb://fake-for-test")
    @patch("dashboard.main.get_db")
    def test_homepage_renders_scoring_failed_job_as_unscored(self, mock_get_db):
        # A SCORING_FAILED job carries score=None (no evaluation happened) plus a
        # distinct flag. It must render as unscored — not as a real 0/100 score,
        # and not crash the page.
        failed_job = {
            "job_id": "unscored-1", "source": "linkedin", "title": "Director of Engineering",
            "company": "Acme", "status": "discovered", "flags": "SCORING_FAILED",
            "score": None, "score_reason": "No model available for scoring",
            "discovered_at": "2026-09-14T00:00:00+00:00", "url": "https://example.com/jobs/789",
        }
        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.jobs.find.side_effect = lambda filt, *a, **kw: (
            _chainable([failed_job]) if filt.get("status") == "discovered" else _chainable([])
        )
        mock_db.jobs.aggregate.return_value = []
        mock_db.jobs.count_documents.return_value = 0
        mock_db.sync_events.find.return_value.sort.return_value.limit.return_value = []
        mock_db.sync_events.find_one.return_value = None
        mock_db.credentials.find.return_value = []

        resp = self.client.get("/", headers=_AUTH)
        self.assertEqual(resp.status_code, 200, f"homepage crashed: {resp.text[:500]}")
        # Assert on the rendered element, not the CSS rule (which also contains
        # the class name and would make this pass even with the badge removed).
        self.assertIn('class="jflag jf-unscored"', resp.text,
                      "SCORING_FAILED should render the UNSCORED badge element")
        self.assertIn(">UNSCORED<", resp.text)


if __name__ == "__main__":
    unittest.main()
