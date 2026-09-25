"""brain_memory_client transport + outbox tests — all offline via
httpx.MockTransport injected through the `client` parameter (dependency
injection; no network), mirroring test_incident_reporter.py's conventions."""
import json

import httpx
import pytest

from src import brain_memory_client as bmc

ENV = {
    "BRAIN_MEMORY_URL": "https://brain.test",
    "BRAIN_MEMORY_KEY_ID": "job-agent-1",
    "BRAIN_MEMORY_SECRET": "test-secret",
}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _set_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


def _clear_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def test_disabled_without_env(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    calls = []

    def handler(request):  # must never be reached
        calls.append(request)
        return httpx.Response(200, json={})

    outbox = tmp_path / "outbox.jsonl"
    assert bmc.is_configured() is False
    bmc.enqueue(
        bmc.MissionContextRecord(kind="outcome", record_id="r1", fields={"technicalSuccess": True}),
        client=_client(handler), outbox_path=outbox,
    )
    assert calls == []
    assert not outbox.exists()


def test_disabled_when_one_var_missing(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("BRAIN_MEMORY_SECRET")
    assert bmc.is_configured() is False


def test_ingest_payload_shape_and_signing(monkeypatch, tmp_path):
    _set_env(monkeypatch)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"batchId": "b1"})

    outbox = tmp_path / "outbox.jsonl"
    bmc.emit_outcome(
        record_id="job-agent-outcome-j1-a1",
        technical_success=True,
        evidence_refs=["job:j1", "attempt:a1"],
        client=_client(handler),
    )
    assert seen["url"] == "https://brain.test/v1/artifacts/ingest"
    assert seen["headers"]["x-brain-key-id"] == "job-agent-1"
    assert "x-brain-request-id" in seen["headers"]
    assert "x-brain-timestamp" in seen["headers"]
    assert len(seen["headers"]["x-brain-signature"]) == 64  # hex sha256

    body = seen["body"]
    assert body["sensitivity"] == "private"
    assert body["permittedAgents"] == ["brain"]
    descriptor = body["descriptor"]
    assert descriptor["mimeType"] == "application/json"
    assert descriptor["scope"] == "job-agent"
    assert descriptor["sourceType"] == "job-agent-outcome"
    assert descriptor["artifactId"] == "job-agent-outcome-j1-a1"

    record = json.loads(body["sourceText"])
    assert record["kind"] == "outcome"
    assert record["technicalSuccess"] is True
    assert record["evidenceRefs"] == ["job:j1", "attempt:a1"]
    assert not outbox.exists()  # delivered on first attempt, never queued


def test_signature_verifies_with_known_vector(monkeypatch):
    # Recomputes the HMAC independently (not just "does the client send SOME
    # signature") to catch a canonical-string or digest-order regression.
    import hashlib
    import hmac as hmac_mod

    _set_env(monkeypatch)
    captured = {}

    def handler(request):
        captured["body"] = request.content.decode("utf-8")
        captured["headers"] = dict(request.headers)
        return httpx.Response(201, json={})

    bmc.emit_goal(record_id="g1", title="Land a staff role", status="active", priority="high", client=_client(handler))

    digest = hashlib.sha256(captured["body"].encode("utf-8")).hexdigest()
    canonical = "\n".join([
        "brain-memory-http-ingest-v1", "POST", "/v1/artifacts/ingest",
        captured["headers"]["x-brain-key-id"],
        captured["headers"]["x-brain-request-id"],
        captured["headers"]["x-brain-timestamp"],
        digest,
    ])
    expected = hmac_mod.new(ENV["BRAIN_MEMORY_SECRET"].encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    assert captured["headers"]["x-brain-signature"] == expected


def test_defaults_to_private_sensitivity(monkeypatch):
    _set_env(monkeypatch)
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={})

    bmc.emit_challenge(record_id="c1", title="Workday extraction misfires", status="active", impact="medium", client=_client(handler))
    assert seen["body"]["sensitivity"] == "private"


@pytest.mark.parametrize("code", [400, 401, 500])
def test_failed_ingest_queues_to_outbox(monkeypatch, tmp_path, code):
    _set_env(monkeypatch)

    def handler(request):
        return httpx.Response(code, json={"error": "nope"})

    outbox = tmp_path / "outbox.jsonl"
    bmc.enqueue(
        bmc.MissionContextRecord(kind="outcome", record_id="r1", fields={"technicalSuccess": False}),
        client=_client(handler), outbox_path=outbox,
    )
    assert outbox.exists()
    rows = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["attempts"] == 1


def test_transport_error_queues_to_outbox(monkeypatch, tmp_path):
    _set_env(monkeypatch)

    def handler(request):
        raise httpx.ConnectError("boom")

    outbox = tmp_path / "outbox.jsonl"
    bmc.enqueue(
        bmc.MissionContextRecord(kind="outcome", record_id="r1", fields={"technicalSuccess": False}),
        client=_client(handler), outbox_path=outbox,
    )
    assert outbox.exists()


def test_flush_pending_delivers_and_clears(monkeypatch, tmp_path):
    _set_env(monkeypatch)
    outbox = tmp_path / "outbox.jsonl"
    envelope = {"descriptor": {"artifactId": "a1"}, "sensitivity": "private", "permittedAgents": ["brain"], "sourceText": "{}"}
    outbox.write_text(json.dumps({"envelope": envelope, "attempts": 2}) + "\n")

    def handler(request):
        return httpx.Response(201, json={})

    result = bmc.flush_pending(client=_client(handler), outbox_path=outbox)
    assert result == {"delivered": 1, "dead": 0, "pending": 0}
    assert outbox.read_text() == ""


def test_flush_pending_dead_letters_after_max_attempts(monkeypatch, tmp_path):
    _set_env(monkeypatch)
    outbox = tmp_path / "outbox.jsonl"
    envelope = {"descriptor": {"artifactId": "a1"}, "sensitivity": "private", "permittedAgents": ["brain"], "sourceText": "{}"}
    outbox.write_text(json.dumps({"envelope": envelope, "attempts": bmc._MAX_ATTEMPTS - 1}) + "\n")

    def handler(request):
        return httpx.Response(500, json={})

    result = bmc.flush_pending(client=_client(handler), outbox_path=outbox)
    assert result == {"delivered": 0, "dead": 1, "pending": 0}
    assert outbox.read_text() == ""


def test_flush_pending_keeps_row_pending_under_max_attempts(monkeypatch, tmp_path):
    _set_env(monkeypatch)
    outbox = tmp_path / "outbox.jsonl"
    envelope = {"descriptor": {"artifactId": "a1"}, "sensitivity": "private", "permittedAgents": ["brain"], "sourceText": "{}"}
    outbox.write_text(json.dumps({"envelope": envelope, "attempts": 1}) + "\n")

    def handler(request):
        return httpx.Response(500, json={})

    result = bmc.flush_pending(client=_client(handler), outbox_path=outbox)
    assert result == {"delivered": 0, "dead": 0, "pending": 1}
    rows = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert rows[0]["attempts"] == 2


def test_flush_pending_noop_when_unconfigured(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text(json.dumps({"envelope": {}, "attempts": 1}) + "\n")

    def handler(request):
        raise AssertionError("must not be called when unconfigured")

    result = bmc.flush_pending(client=_client(handler), outbox_path=outbox)
    assert result == {"delivered": 0, "dead": 0, "pending": 0}
    # Left untouched — flush_pending must not silently drop queued work just
    # because credentials are temporarily absent from this process's env.
    assert outbox.exists()


def test_flush_pending_noop_on_empty_outbox(monkeypatch, tmp_path):
    _set_env(monkeypatch)
    outbox = tmp_path / "outbox.jsonl"

    def handler(request):
        raise AssertionError("must not be called with nothing queued")

    result = bmc.flush_pending(client=_client(handler), outbox_path=outbox)
    assert result == {"delivered": 0, "dead": 0, "pending": 0}
