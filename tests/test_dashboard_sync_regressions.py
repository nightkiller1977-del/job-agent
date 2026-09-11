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
"""
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

with patch.dict("os.environ", {"MONGODB_URI": "", "SYNC_SECRET": "testsecret"}):
    from dashboard.main import app


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


class TestExternalJobScorePlaceholder(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("dashboard.main.get_db")
    def test_external_job_is_inserted_with_score_none_not_missing(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.jobs.find_one.return_value = None  # not a duplicate
        mock_get_db.return_value = mock_db

        resp = self.client.post("/api/jobs/external", json={"url": "https://example.com/jobs/123"})
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

        post_resp = self.client.post("/api/jobs/external", json={"url": "https://example.com/jobs/456"})
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

        get_resp = self.client.get("/")
        self.assertEqual(
            get_resp.status_code, 200,
            f"homepage crashed rendering a job with score=None: {get_resp.text[:500]}",
        )
        self.assertIn(inserted_job["job_id"], get_resp.text)


if __name__ == "__main__":
    unittest.main()
