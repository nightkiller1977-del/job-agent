import base64

from src.telemetry import resolve_loki_auth, resolve_loki_url

REMOTE_URL = "https://logs.example.grafana.net/loki/api/v1/push"
AUTH = "Basic " + base64.b64encode(b"123456:secret-token").decode("ascii")


def _enable_remote(monkeypatch):
    """Complete valid pair + explicit dev/test opt-in (ACES-293 policy)."""
    monkeypatch.setenv("LOKI_URL_REMOTE", REMOTE_URL)
    monkeypatch.setenv("LOKI_REMOTE_AUTH", AUTH)
    monkeypatch.setenv("OBSERVABILITY_REMOTE", "1")


def test_remote_loki_url_takes_precedence_when_pair_enabled(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://localhost:3100/loki/api/v1/push")
    _enable_remote(monkeypatch)
    assert resolve_loki_url() == REMOTE_URL
    assert resolve_loki_auth() == ("123456", "secret-token")


def test_partial_pair_disables_remote_and_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://localhost:3100/loki/api/v1/push")
    monkeypatch.setenv("LOKI_URL_REMOTE", REMOTE_URL)
    monkeypatch.delenv("LOKI_REMOTE_AUTH", raising=False)
    monkeypatch.setenv("OBSERVABILITY_REMOTE", "1")
    assert resolve_loki_url() == "http://localhost:3100/loki/api/v1/push"
    assert resolve_loki_auth() is None


def test_legacy_split_auth_is_retired(monkeypatch):
    monkeypatch.delenv("LOKI_URL_REMOTE", raising=False)
    monkeypatch.delenv("LOKI_REMOTE_AUTH", raising=False)
    monkeypatch.setenv("LOKI_USER", "123456")
    monkeypatch.setenv("LOKI_API_KEY", "secret-token")
    monkeypatch.setenv("OBSERVABILITY_REMOTE", "1")
    assert resolve_loki_auth() is None


def test_invalid_shared_auth_fails_closed(monkeypatch):
    monkeypatch.setenv("LOKI_URL_REMOTE", REMOTE_URL)
    monkeypatch.setenv("LOKI_REMOTE_AUTH", "Basic not-valid-base64!!!")
    monkeypatch.setenv("OBSERVABILITY_REMOTE", "1")
    assert resolve_loki_auth() is None
    assert resolve_loki_url() == "http://localhost:3100/loki/api/v1/push"


def test_missing_render_signal_defaults_to_production(monkeypatch):
    # ACES-461: post-Render-migration, a completely absent RENDER (the Azure
    # reality) must still auto-enable a valid pair, not silently disable it.
    monkeypatch.delenv("OBSERVABILITY_REMOTE", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("LOKI_URL_REMOTE", REMOTE_URL)
    monkeypatch.setenv("LOKI_REMOTE_AUTH", AUTH)
    monkeypatch.delenv("LOKI_URL", raising=False)
    assert resolve_loki_auth() == ("123456", "secret-token")
    assert resolve_loki_url() == REMOTE_URL


def test_render_present_but_falsy_stays_non_production_default_off(monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_REMOTE", raising=False)
    monkeypatch.setenv("RENDER", "")
    monkeypatch.setenv("LOKI_URL_REMOTE", REMOTE_URL)
    monkeypatch.setenv("LOKI_REMOTE_AUTH", AUTH)
    monkeypatch.delenv("LOKI_URL", raising=False)
    assert resolve_loki_auth() is None
    assert resolve_loki_url() == "http://localhost:3100/loki/api/v1/push"


def test_production_auto_on_and_opt_out(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("LOKI_URL_REMOTE", REMOTE_URL)
    monkeypatch.setenv("LOKI_REMOTE_AUTH", AUTH)
    monkeypatch.delenv("OBSERVABILITY_REMOTE", raising=False)
    assert resolve_loki_url() == REMOTE_URL
    monkeypatch.setenv("OBSERVABILITY_REMOTE", "0")
    assert resolve_loki_auth() is None


def test_local_loki_remains_default(monkeypatch):
    monkeypatch.delenv("LOKI_URL_REMOTE", raising=False)
    monkeypatch.delenv("LOKI_URL", raising=False)
    assert resolve_loki_url() == "http://localhost:3100/loki/api/v1/push"
