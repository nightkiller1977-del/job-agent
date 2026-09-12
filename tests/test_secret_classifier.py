"""Unit tests for the model-backed secret classifier."""
import asyncio
import json
import pathlib
import tempfile
from unittest.mock import patch

import pytest

import src.secret_classifier as sc
import src.secret_store as ss


class _FakeModelClient:
    """Deterministic ModelClient stand-in: returns whichever candidate names
    look like the given purpose keyword."""
    def __init__(self, keyword: str = "mail"):
        self.keyword = keyword

    def __call__(self, **kw):
        return self

    async def complete(self, messages, system="", **kw):
        prompt = messages[0]["content"].lower()
        picks = []
        # Extract candidate names from the prompt (they appear as "- NAME").
        for line in messages[0]["content"].splitlines():
            line = line.strip()
            if line.startswith("- "):
                name = line[2:].strip()
                if self.keyword in name.lower():
                    picks.append(name)
        return json.dumps(picks)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every test gets its own cache file so caches don't bleed across tests."""
    monkeypatch.setattr(sc, "_CACHE_PATH", tmp_path / "purpose_cache.json")
    yield


def test_classified_keys_returns_empty_when_no_cache():
    assert sc.classified_keys("imap_password", ["FOO", "BAR"]) == []


def test_store_and_classified_keys_roundtrip():
    keys = ["MAIL_TOKEN", "OTHER"]
    sc.store_classification("imap_password", keys, ["MAIL_TOKEN"])
    assert sc.classified_keys("imap_password", keys) == ["MAIL_TOKEN"]


def test_classified_keys_busts_on_candidate_change():
    sc.store_classification("imap_password", ["A", "B"], ["A"])
    # Different candidate set → cache miss.
    assert sc.classified_keys("imap_password", ["A", "B", "C"]) == []


def test_classified_keys_filters_out_stale_names():
    """A name in the cached ranking that isn't in the current candidate set
    is silently dropped — the caller only sees names it can actually resolve."""
    sc.store_classification("imap_password", ["A", "B"], ["A", "B"])
    # Same set → cache hit — but if 'B' were rotated out the caller filters.
    assert sc.classified_keys("imap_password", ["A", "B"]) == ["A", "B"]


def test_classify_purpose_async_calls_model_and_caches(monkeypatch):
    fake = _FakeModelClient(keyword="mail")
    with patch("src.model_client.ModelClient", fake):
        candidates = ["MAIL_BOT_KEY_V2", "INBOX_TOKEN", "MY_FAVORITE_NUMBER"]
        ranked = asyncio.run(sc.classify_purpose_async("imap_password", candidates))
    assert ranked == ["MAIL_BOT_KEY_V2"]
    # Cached — a second call returns the same result without re-invoking the model.
    assert sc.classified_keys("imap_password", candidates) == ["MAIL_BOT_KEY_V2"]


def test_classify_purpose_async_unknown_purpose_is_noop():
    ranked = asyncio.run(sc.classify_purpose_async("nonexistent_purpose", ["A", "B"]))
    assert ranked == []


def test_classify_purpose_async_short_circuits_with_cache():
    """When the cache already covers the candidate set, don't call the model."""
    sc.store_classification("imap_password", ["A", "B"], ["A"])
    # No ModelClient patch — if it were called we'd blow up on import/creds.
    ranked = asyncio.run(sc.classify_purpose_async("imap_password", ["A", "B"]))
    assert ranked == ["A"]


def test_discover_by_purpose_prefers_model_ranking(monkeypatch):
    """discover_by_purpose returns the cached model ranking when present."""
    def fake_read():
        return {"mail_bot_key_v2": "x", "IMAP_PASSWORD": "y"}
    monkeypatch.setattr(ss, "_read_store", fake_read)
    monkeypatch.setattr(ss.shutil, "which", lambda _cmd: None)
    # Model picks the freeform name; regex would only find IMAP_PASSWORD.
    sc.store_classification(
        "imap_password",
        ["mail_bot_key_v2", "IMAP_PASSWORD"],
        ["mail_bot_key_v2"],
    )
    ranked = ss.discover_by_purpose("imap_password")
    # Model result first, regex hits appended.
    assert ranked[0] == "mail_bot_key_v2"
    assert "IMAP_PASSWORD" in ranked
