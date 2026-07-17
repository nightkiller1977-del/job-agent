"""Tests for the single-source secrets resolver (src/secret_store.py).

Covers the two invariants that the credential band-aids only approximated:
  * fill-missing, EMPTY-STRING-treated-as-absent (the Claude Code ANTHROPIC_API_KEY=""
    trap that a plain override=False load would miss)
  * never overwrite a real value already in os.environ
"""
from __future__ import annotations

import os

import pytest

from src import secret_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the resolver at a temp central store and clear its cache."""
    monkeypatch.setenv("AICC_SECRETS_DIR", str(tmp_path))
    secret_store.clear_cache()

    def _write(**pairs):
        lines = [f"{k}={v}" for k, v in pairs.items()]
        (tmp_path / ".env").write_text("\n".join(lines) + "\n")
        secret_store.clear_cache()

    yield _write
    secret_store.clear_cache()


def test_fills_absent_key(store, monkeypatch):
    monkeypatch.delenv("JOBRIGHT_EMAIL", raising=False)
    store(JOBRIGHT_EMAIL="from-store@example.com")

    filled = secret_store.fill_missing(["JOBRIGHT_EMAIL"])

    assert filled == ["JOBRIGHT_EMAIL"]
    assert os.environ["JOBRIGHT_EMAIL"] == "from-store@example.com"


def test_fills_empty_string_key(store, monkeypatch):
    # The exact failure mode: shell sets the var to "" and a naive override=False
    # would consider it "present" and refuse to fill.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    store(ANTHROPIC_API_KEY="sk-real")

    secret_store.fill_missing(["ANTHROPIC_API_KEY"])

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-real"


def test_never_overrides_real_value(store, monkeypatch):
    monkeypatch.setenv("LINKEDIN_PASSWORD", "local-wins")
    store(LINKEDIN_PASSWORD="store-loses")

    filled = secret_store.fill_missing(["LINKEDIN_PASSWORD"])

    assert filled == []
    assert os.environ["LINKEDIN_PASSWORD"] == "local-wins"


def test_resolve_secret_does_not_mutate_env(store, monkeypatch):
    monkeypatch.delenv("USAJOBS_EMAIL", raising=False)
    store(USAJOBS_EMAIL="a@b.com")

    assert secret_store.resolve_secret("USAJOBS_EMAIL") == "a@b.com"
    assert "USAJOBS_EMAIL" not in os.environ  # resolve is read-only


def test_missing_everywhere_returns_none(store, monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    store(SOMETHING_ELSE="x")

    assert secret_store.resolve_secret("NOPE_KEY") is None
    assert secret_store.fill_missing(["NOPE_KEY"]) == []


def test_absent_store_degrades_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("AICC_SECRETS_DIR", str(tmp_path))  # empty dir, no .env
    secret_store.clear_cache()
    monkeypatch.delenv("JOBRIGHT_EMAIL", raising=False)

    assert secret_store.fill_missing(["JOBRIGHT_EMAIL"]) == []


def test_quoted_and_export_lines_parsed(store, monkeypatch):
    monkeypatch.delenv("SYNC_SECRET", raising=False)
    (secret_store._commander_dir() / ".env").write_text(
        'export SYNC_SECRET="quoted-value"\n# a comment\n\nBLANK\n'
    )
    secret_store.clear_cache()

    assert secret_store.resolve_secret("SYNC_SECRET") == "quoted-value"


def test_default_fill_includes_imap_password(store, monkeypatch):
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)
    store(IMAP_PASSWORD="imap-app-password")

    filled = secret_store.fill_missing()

    assert "IMAP_PASSWORD" in filled
    assert os.environ["IMAP_PASSWORD"] == "imap-app-password"
