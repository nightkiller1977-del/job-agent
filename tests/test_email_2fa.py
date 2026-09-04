import imaplib
import time

import pytest
from unittest.mock import patch, MagicMock
from src.email_helper import (
    get_imap_server_for_email,
    imap_password_candidates,
    is_imap_auth_failure,
    resolve_imap_credentials,
    retrieve_email_2fa_code,
)

_ALL_IMAP_KEYS = (
    "EMAIL_2FA_ADDRESS", "USAJOBS_EMAIL", "IMAP_USER",
    "IMAP_PASSWORD", "ICLOUD_APP_PASSWORD_PERSONAL", "ICLOUD_APP_PASSWORD",
    "ICLOUD_APP_PASSWORD_ICLOUD", "ICLOUD_APP_PASSWORD_MAC", "USAJOBS_PASSWORD",
)


def _clear(monkeypatch):
    for key in _ALL_IMAP_KEYS:
        monkeypatch.delenv(key, raising=False)


# ── ACES-283: IMAP credential resolution ────────────────────────────────────────

def test_resolve_never_falls_back_to_the_site_login_password(monkeypatch):
    """The old reader used USAJOBS_PASSWORD (the login.gov password) for iCloud IMAP
    when no app password was set. iCloud always rejects that, so every 2FA attempt
    became AUTHENTICATIONFAILED and silently degraded to the human path."""
    _clear(monkeypatch)
    monkeypatch.setenv("USAJOBS_EMAIL", "me@icloud.com")
    monkeypatch.setenv("USAJOBS_PASSWORD", "login-gov-password")
    assert resolve_imap_credentials() == ("me@icloud.com", "")


def test_resolve_accepts_the_central_store_icloud_key_names(monkeypatch):
    """aicc-secrets stores the two iCloud app passwords as ICLOUD_APP_PASSWORD_ICLOUD /
    _MAC — names the old reader did not know, so a valid credential sat unused."""
    _clear(monkeypatch)
    monkeypatch.setenv("USAJOBS_EMAIL", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD_ICLOUD", "app-pw-icloud")
    assert resolve_imap_credentials() == ("me@icloud.com", "app-pw-icloud")


def test_resolve_prefers_canonical_imap_password(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("USAJOBS_EMAIL", "me@icloud.com")
    monkeypatch.setenv("IMAP_PASSWORD", "canonical")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD_ICLOUD", "store-name")
    assert resolve_imap_credentials()[1] == "canonical"


def test_resolve_explicit_arguments_win(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("USAJOBS_EMAIL", "env@icloud.com")
    monkeypatch.setenv("IMAP_PASSWORD", "env-pw")
    assert resolve_imap_credentials("arg@icloud.com", "arg-pw") == ("arg@icloud.com", "arg-pw")


def test_icloud_keys_are_never_used_for_other_providers(monkeypatch):
    """An iCloud app password handed to Gmail can only fail auth; a Gmail address with
    only ICLOUD_* keys set must resolve to 'no password' instead (Copilot, PR #88)."""
    _clear(monkeypatch)
    monkeypatch.setenv("USAJOBS_EMAIL", "me@gmail.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD_PERSONAL", "icloud-only")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "icloud-only-2")
    assert resolve_imap_credentials() == ("me@gmail.com", "")
    monkeypatch.setenv("IMAP_PASSWORD", "generic")
    assert resolve_imap_credentials()[1] == "generic"


def test_candidates_are_domain_aware():
    assert imap_password_candidates("x@icloud.com")[-2:] == ("ICLOUD_APP_PASSWORD_ICLOUD", "ICLOUD_APP_PASSWORD_MAC")
    assert imap_password_candidates("x@mac.com")[-2:] == ("ICLOUD_APP_PASSWORD_MAC", "ICLOUD_APP_PASSWORD_ICLOUD")
    assert imap_password_candidates("x@me.com")[-2:] == ("ICLOUD_APP_PASSWORD_MAC", "ICLOUD_APP_PASSWORD_ICLOUD")
    gmail = imap_password_candidates("x@gmail.com")
    assert gmail == ("IMAP_PASSWORD",)
    assert imap_password_candidates("x@icloud.com")[:3] == (
        "IMAP_PASSWORD", "ICLOUD_APP_PASSWORD_PERSONAL", "ICLOUD_APP_PASSWORD",
    )
    for keys in (imap_password_candidates(a) for a in ("x@icloud.com", "x@gmail.com", "")):
        assert "USAJOBS_PASSWORD" not in keys


def test_is_imap_auth_failure_recognises_rejections_only():
    assert is_imap_auth_failure(imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed."))
    assert is_imap_auth_failure(imaplib.IMAP4.error("LOGIN failed."))
    assert not is_imap_auth_failure(imaplib.IMAP4.error("connection reset by peer"))
    assert not is_imap_auth_failure(TimeoutError("timed out"))


@patch("imaplib.IMAP4_SSL")
def test_rejected_credentials_fail_fast_instead_of_polling(mock_imap):
    """A rejected app password will not fix itself within the 2FA window: one login
    attempt, one warning, return None — not a login every 10 s for `timeout` seconds."""
    instance = MagicMock()
    mock_imap.return_value = instance
    instance.login.side_effect = imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")

    started = time.monotonic()
    code = retrieve_email_2fa_code("me@icloud.com", "bad-pw", "login.gov", "security code", timeout=60)

    assert code is None
    assert instance.login.call_count == 1
    assert time.monotonic() - started < 5


@patch("imaplib.IMAP4_SSL")
def test_transient_login_error_still_retries(mock_imap):
    instance = MagicMock()
    mock_imap.return_value = instance
    instance.login.side_effect = [imaplib.IMAP4.error("connection reset by peer"), None]
    instance.search.return_value = ("OK", [b"1"])
    instance.fetch.return_value = ("OK", [(None, b"Subject: code\n\nYour code is 123456.")])

    with patch("src.email_helper.time.sleep"):
        code = retrieve_email_2fa_code("me@gmail.com", "pw", "login.gov", "code", timeout=60)

    assert code == "123456"
    assert instance.login.call_count == 2

def test_get_imap_server_for_email():
    assert get_imap_server_for_email("test@gmail.com") == "imap.gmail.com"
    assert get_imap_server_for_email("test@yahoo.com") == "imap.mail.yahoo.com"
    assert get_imap_server_for_email("test@icloud.com") == "imap.mail.me.com"
    assert get_imap_server_for_email("test@outlook.com") == "outlook.office365.com"
    assert get_imap_server_for_email("test@unknown.com") == ""

@patch("imaplib.IMAP4_SSL")
def test_retrieve_email_2fa_code_success(mock_imap):
    # Mock IMAP client instance
    instance = MagicMock()
    mock_imap.return_value = instance
    
    # Mock search results
    instance.search.return_value = ("OK", [b"1 2 3"])
    
    # Mock fetch results: RFC822 mail payload containing code 554321
    email_body = (
        b"From: no-reply@login.gov\n"
        b"Subject: Your security code\n"
        b"\n"
        b"Your login.gov security code is: 554321. Do not share this."
    )
    instance.fetch.return_value = ("OK", [(None, email_body)])
    
    code = retrieve_email_2fa_code(
        email_address="test@gmail.com",
        password="app-password-mock",
        sender_pattern="login.gov",
        subject_pattern="security code",
        timeout=5
    )
    
    assert code == "554321"
    instance.login.assert_called_once_with("test@gmail.com", "app-password-mock")
    instance.select.assert_called_once_with("INBOX")
