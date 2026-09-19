from src.reauth import classify_session_failure


def test_email_timeout_remains_primary_when_notification_is_unavailable():
    outcome = classify_session_failure(
        TimeoutError("mail code timed out"), notification_error=OSError("missing notifier")
    )

    assert outcome["primary_failure"]["kind"] == "email_code_timeout"
    assert outcome["secondary_conditions"] == [{"kind": "notification_unavailable", "operation": "platform_notification"}]


def test_captcha_and_two_factor_remain_distinct_session_failures():
    assert classify_session_failure(RuntimeError("CAPTCHA challenge"))["primary_failure"]["kind"] == "captcha"
    assert classify_session_failure(RuntimeError("two-factor authentication required"))["primary_failure"]["kind"] == "two_factor_required"
