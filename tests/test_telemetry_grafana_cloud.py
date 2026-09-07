import base64

from src.telemetry import resolve_loki_auth, resolve_loki_url


def test_remote_loki_url_takes_precedence(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://localhost:3100/loki/api/v1/push")
    monkeypatch.setenv("LOKI_URL_REMOTE", "https://logs.example.grafana.net/loki/api/v1/push")
    assert resolve_loki_url() == "https://logs.example.grafana.net/loki/api/v1/push"


def test_shared_basic_auth_contract_takes_precedence(monkeypatch):
    encoded = base64.b64encode(b"123456:secret-token").decode("ascii")
    monkeypatch.setenv("LOKI_REMOTE_AUTH", f"Basic {encoded}")
    monkeypatch.setenv("LOKI_USER", "legacy-user")
    monkeypatch.setenv("LOKI_API_KEY", "legacy-key")
    assert resolve_loki_auth() == ("123456", "secret-token")


def test_legacy_split_auth_still_supported(monkeypatch):
    monkeypatch.delenv("LOKI_REMOTE_AUTH", raising=False)
    monkeypatch.setenv("LOKI_USER", "123456")
    monkeypatch.setenv("LOKI_API_KEY", "secret-token")
    assert resolve_loki_auth() == ("123456", "secret-token")


def test_invalid_shared_auth_fails_closed_without_split_fallback(monkeypatch):
    monkeypatch.setenv("LOKI_REMOTE_AUTH", "Basic not-valid-base64!!!")
    monkeypatch.delenv("LOKI_USER", raising=False)
    monkeypatch.delenv("LOKI_API_KEY", raising=False)
    assert resolve_loki_auth() is None


def test_local_loki_remains_default(monkeypatch):
    monkeypatch.delenv("LOKI_URL_REMOTE", raising=False)
    monkeypatch.delenv("LOKI_URL", raising=False)
    assert resolve_loki_url() == "http://localhost:3100/loki/api/v1/push"
