from src.telemetry import resolve_loki_auth, resolve_loki_url


def test_remote_loki_url_takes_precedence(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://localhost:3100/loki/api/v1/push")
    monkeypatch.setenv("LOKI_URL_REMOTE", "https://logs.example.grafana.net/loki/api/v1/push")

    assert resolve_loki_url() == "https://logs.example.grafana.net/loki/api/v1/push"


def test_grafana_cloud_basic_auth_requires_both_values(monkeypatch):
    monkeypatch.setenv("LOKI_USER", "123456")
    monkeypatch.setenv("LOKI_API_KEY", "secret-token")
    assert resolve_loki_auth() == ("123456", "secret-token")

    monkeypatch.delenv("LOKI_API_KEY")
    assert resolve_loki_auth() is None


def test_local_loki_remains_default(monkeypatch):
    monkeypatch.delenv("LOKI_URL_REMOTE", raising=False)
    monkeypatch.delenv("LOKI_URL", raising=False)

    assert resolve_loki_url() == "http://localhost:3100/loki/api/v1/push"
