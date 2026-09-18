"""Regression tests for bounded, redacted operational-failure records."""

import socket

import pytest

from src.operational_failure import describe_failure, is_retry_authorized


def test_empty_timeout_has_safe_nonblank_record():
    record = describe_failure("cloud_pull_approved", "dashboard_read", TimeoutError(""))

    assert record == {
        "operation": "cloud_pull_approved",
        "endpoint_class": "dashboard_read",
        "kind": "timeout",
        "message": "timeout",
        "retryable_transport": True,
    }


def test_nested_cause_redacts_url_query_and_secret():
    cause = ValueError("Authorization: Bearer top-secret")
    error = RuntimeError("request failed at https://dashboard.example/api?token=top-secret")
    error.__cause__ = cause

    record = describe_failure("cloud_sync_jobs", "dashboard_sync", error)

    assert "https://" not in record["message"]
    assert "token" not in record["message"].lower()
    assert "secret" not in record["message"].lower()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (socket.gaierror(-3, "name resolution failed"), "dns"),
        (ConnectionRefusedError("refused"), "connect"),
    ],
)
def test_known_transport_failures_are_classified(exc, expected):
    assert describe_failure("cloud_pull_approved", "dashboard_read", exc)["kind"] == expected


def test_unknown_operation_is_rejected():
    with pytest.raises(ValueError, match="operation"):
        describe_failure("https://dashboard.example/api", "dashboard_read", TimeoutError())


def test_state_changing_action_is_never_retry_authorized():
    assert not is_retry_authorized(
        "cloud_action",
        idempotent=False,
        state_changing=True,
        submission_state=None,
        attempts=1,
        max_attempts=2,
        breaker_allows=True,
    )


def test_submission_uncertainty_blocks_even_idempotent_operation_retry():
    assert not is_retry_authorized(
        "cloud_sync_jobs",
        idempotent=True,
        state_changing=False,
        submission_state="submission_unverified",
        attempts=0,
        max_attempts=2,
        breaker_allows=True,
    )
