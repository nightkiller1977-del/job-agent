import pytest
from unittest.mock import patch, MagicMock
from src.email_helper import get_imap_server_for_email, retrieve_email_2fa_code

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
