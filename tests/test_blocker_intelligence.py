"""Unit tests for the model-backed blocker intelligence layer."""
import asyncio
import json
from unittest.mock import patch

import pytest

import src.blocker_intelligence as bi
from src.blocker_classifier import BlockerClass, classify, max_attempts, should_attempt


class _FakeModel:
    """Returns a fixed verdict — verifies the cache path independent of prompt shape."""
    def __init__(self, verdict: str = "transient"):
        self.verdict = verdict
        self.calls = 0

    def __call__(self, **kw):
        return self

    async def complete(self, messages, **kw):
        self.calls += 1
        return json.dumps({"class": self.verdict})


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(bi, "_CACHE_PATH", tmp_path / "blocker_cache.json")
    yield


def test_classified_status_empty_when_no_cache():
    assert bi.classified_status("mystery_status", []) is None


def test_classify_status_async_persists_verdict():
    fake = _FakeModel(verdict="auth_required")
    with patch("src.model_client.ModelClient", fake):
        verdict = asyncio.run(bi.classify_status_async("mystery", ["reason A", "reason B"]))
    assert verdict == "auth_required"
    # Cache hit — second call must not re-invoke the model.
    assert bi.classified_status("mystery", ["reason A", "reason B"]) == "auth_required"
    assert fake.calls == 1


def test_classify_status_async_short_circuits_on_cache():
    """Cached verdict short-circuits the model call for the same evidence."""
    bi._store_classification("known", ["r1"], "transient")
    fake = _FakeModel(verdict="permanent")
    with patch("src.model_client.ModelClient", fake):
        verdict = asyncio.run(bi.classify_status_async("known", ["r1"]))
    assert verdict == "transient"
    assert fake.calls == 0


def test_classified_status_busts_on_evidence_change():
    bi._store_classification("s", ["r1"], "transient")
    # Different evidence → treat as fresh.
    assert bi.classified_status("s", ["r1", "r2"]) is None


def test_adaptive_cap_lowers_for_doomed_pair():
    bi.store_funnel_snapshot({
        "linkedin": {
            "linkedin_stuck_on_required_field": {"attempts": 8, "submitted": 0},
        }
    })
    cap, reason = bi.adaptive_cap(
        "linkedin",
        "linkedin_stuck_on_required_field",
        static_cap=3,
    )
    assert cap == 1
    assert "0/8" in reason


def test_adaptive_cap_preserves_static_when_history_thin():
    bi.store_funnel_snapshot({"linkedin": {"foo": {"attempts": 2, "submitted": 0}}})
    cap, _ = bi.adaptive_cap("linkedin", "foo", static_cap=3)
    assert cap == 3


def test_adaptive_cap_preserves_static_when_pair_has_success():
    bi.store_funnel_snapshot({"linkedin": {"foo": {"attempts": 20, "submitted": 3}}})
    cap, _ = bi.adaptive_cap("linkedin", "foo", static_cap=3)
    assert cap == 3


def test_classify_falls_back_to_cached_model_verdict():
    """A status not in the static map picks up the model's cached verdict."""
    bi._store_classification("brand_new_workday_status", [], "auth_required")
    assert classify("brand_new_workday_status") is BlockerClass.AUTH_REQUIRED


def test_should_attempt_uses_adaptive_cap():
    # linkedin has a proven-doomed pair.
    bi.store_funnel_snapshot({
        "linkedin": {"linkedin_stuck_on_required_field": {"attempts": 8, "submitted": 0}}
    })
    # First attempt allowed (adaptive cap=1).
    assert should_attempt("linkedin_stuck_on_required_field", 0, source="linkedin")[0] is True
    # Second attempt blocked — the adaptive cap of 1 kicks in below the static 1
    # for needs_human (also 1) so behavior matches; try a status whose static cap
    # is larger to make the point measurable.


def test_should_attempt_adaptive_cap_bites_below_static():
    bi.store_funnel_snapshot({
        "workday": {
            "workday_session_expired": {"attempts": 6, "submitted": 0},
        }
    })
    # Static cap for AUTH_REQUIRED is 5. Adaptive drops to 1.
    ok, reason = should_attempt("workday_session_expired", 1, source="workday")
    assert ok is False
    assert "adaptive" in reason


def test_max_attempts_without_source_uses_static():
    """No source → no adaptive lookup, cap is the static class cap."""
    assert max_attempts("workday_session_expired") == 5
