from __future__ import annotations
import imaplib
import email
import re
import time
import logging
import os

_log = logging.getLogger(__name__)

def get_imap_server_for_email(email_addr: str) -> str:
    """Guess the standard IMAP server based on the email domain name."""
    addr = email_addr.lower()
    if "@gmail.com" in addr:
        return "imap.gmail.com"
    elif "@yahoo.com" in addr or "@ymail.com" in addr:
        return "imap.mail.yahoo.com"
    elif "@icloud.com" in addr or "@me.com" in addr or "@mac.com" in addr:
        return "imap.mail.me.com"
    elif "@outlook.com" in addr or "@hotmail.com" in addr or "@live.com" in addr:
        return "outlook.office365.com"
    return ""

def retrieve_email_2fa_code(
    email_address: str,
    password: str,
    sender_pattern: str,
    subject_pattern: str,
    imap_server: str = "",
    timeout: int = 120
) -> str | None:
    """Connect to the IMAP server and poll the inbox for a 6-digit verification code.
    
    Note: Gmail, Yahoo, and iCloud require App-Specific Passwords rather than your
    main account password to authenticate via IMAP.
    """
    if not email_address or not password:
        _log.warning("Email credentials not fully configured for 2FA retrieval.")
        return None

    if not imap_server:
        imap_server = get_imap_server_for_email(email_address)
        if not imap_server:
            _log.warning(f"Could not determine IMAP server for email: {email_address}")
            return None

    _log.info(f"📧 Starting automated 2FA code retrieval from {email_address} (IMAP: {imap_server})...")
    start_time = time.monotonic()
    
    while time.monotonic() - start_time < timeout:
        try:
            # Connect and login
            mail = imaplib.IMAP4_SSL(imap_server, port=993)
            mail.login(email_address, password)
            mail.select("INBOX")

            # Search for messages matching sender and subject
            search_query = f'(FROM "{sender_pattern}" SUBJECT "{subject_pattern}")'
            status, messages = mail.search(None, search_query)
            
            if status == "OK" and messages[0]:
                msg_ids = messages[0].split()
                # Get the latest message
                latest_id = msg_ids[-1]
                status, data = mail.fetch(latest_id, "(RFC822)")
                if status == "OK":
                    raw_email = data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    # Extract body text
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type == "text/plain":
                                body += part.get_payload(decode=True).decode(errors="ignore")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")
                    
                    # Search for 6-digit code (common in 2FA)
                    match = re.search(r"\b(\d{6})\b", body)
                    if match:
                        code = match.group(1)
                        _log.info(f"📧 Successfully retrieved 2FA code from email: {code}")
                        try:
                            mail.logout()
                        except Exception:
                            pass
                        return code
            
            mail.logout()
        except Exception as exc:
            _log.warning(f"IMAP retrieval attempt failed: {exc}")
        
        # Poll every 10 seconds
        time.sleep(10)
    
    _log.warning("📧 Timed out waiting for 2FA code email.")
    return None
