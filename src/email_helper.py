from __future__ import annotations
import imaplib
import smtplib
import ssl
import email
import re
import time
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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

def get_smtp_server_for_email(email_addr: str) -> tuple[str, int]:
    """Return (host, port) for SMTP SSL based on email domain."""
    addr = email_addr.lower()
    if "@gmail.com" in addr:
        return ("smtp.gmail.com", 465)
    elif "@yahoo.com" in addr or "@ymail.com" in addr:
        return ("smtp.mail.yahoo.com", 465)
    elif "@icloud.com" in addr or "@me.com" in addr or "@mac.com" in addr:
        return ("smtp.mail.me.com", 587)
    elif "@outlook.com" in addr or "@hotmail.com" in addr or "@live.com" in addr:
        return ("smtp.office365.com", 587)
    return ("", 0)

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

    _log.info("mail.2fa_start imap=%s timeout=%d", imap_server, timeout)
    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout:
        try:
            mail = imaplib.IMAP4_SSL(imap_server, port=993)
            mail.login(email_address, password)
            mail.select("INBOX")

            search_query = f'(FROM "{sender_pattern}" SUBJECT "{subject_pattern}")'
            status, messages = mail.search(None, search_query)

            if status == "OK" and messages[0]:
                msg_ids = messages[0].split()
                latest_id = msg_ids[-1]
                status, data = mail.fetch(latest_id, "(RFC822)")
                if status == "OK":
                    raw_email = data[0][1]
                    msg = email.message_from_bytes(raw_email)

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True).decode(errors="ignore")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")

                    match = re.search(r"\b(\d{6})\b", body)
                    if match:
                        code = match.group(1)
                        elapsed = round(time.monotonic() - start_time)
                        _log.info("mail.2fa_found imap=%s elapsed_s=%d", imap_server, elapsed)
                        try:
                            mail.logout()
                        except Exception:
                            pass
                        return code

            mail.logout()
        except Exception as exc:
            _log.warning("mail.2fa_error imap=%s error=%s", imap_server, exc)

        time.sleep(10)

    elapsed = round(time.monotonic() - start_time)
    _log.warning("mail.2fa_timeout imap=%s waited_s=%d", imap_server, elapsed)
    return None


def send_approval_email(
    from_email: str,
    from_password: str,
    to_email: str,
    subject: str,
    body: str,
) -> None:
    """Send an approval request email via SMTP SSL.

    Raises on failure so the caller can surface the error.
    """
    smtp_host, smtp_port = get_smtp_server_for_email(from_email)
    if not smtp_host:
        raise ValueError(f"Could not determine SMTP server for {from_email}")

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    ctx = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx) as server:
            server.login(from_email, from_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=ctx)
            server.login(from_email, from_password)
            server.send_message(msg)

    _log.info("mail.approval_sent from=%s to=%s subject=%r", from_email, to_email, subject)


def poll_for_approval_reply(
    poll_email: str,
    poll_password: str,
    subject_token: str,
    timeout: int = 1800,
    poll_interval: int = 15,
) -> str | None:
    """Poll INBOX for a reply containing an approval choice.

    Searches for messages whose subject contains `subject_token` (a unique ID
    embedded when the approval email was sent). Returns "1"–"9" for approval,
    "denied" for denial, None for timeout.

    The poll_email should be the SENDER account (replies land in its INBOX).
    """
    imap_server = get_imap_server_for_email(poll_email)
    if not imap_server:
        raise ValueError(f"Could not determine IMAP server for {poll_email}")

    _log.info("mail.approval_poll_start token=%s timeout=%d", subject_token, timeout)
    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout:
        try:
            mail = imaplib.IMAP4_SSL(imap_server, port=993)
            mail.login(poll_email, poll_password)
            mail.select("INBOX")

            status, messages = mail.search(None, f'SUBJECT "{subject_token}"')
            if status == "OK" and messages[0]:
                for num in reversed(messages[0].split()):
                    _, data = mail.fetch(num, "(RFC822)")
                    msg = email.message_from_bytes(data[0][1])

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True).decode(errors="ignore")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")

                    # Strip quoted original message (lines starting with >)
                    reply_lines = [l for l in body.splitlines() if not l.startswith(">")]
                    reply_body = " ".join(reply_lines).strip().lower()
                    if not reply_body:
                        continue

                    if re.search(r"\b(deny|no|cancel|reject|stop)\b", reply_body):
                        _log.info("mail.approval_denied token=%s", subject_token)
                        mail.logout()
                        return "denied"

                    m = re.search(r"\b([1-9])\b", reply_body)
                    if m:
                        _log.info("mail.approval_received token=%s option=%s", subject_token, m.group(1))
                        mail.logout()
                        return m.group(1)

            mail.logout()
        except Exception as exc:
            _log.warning("mail.approval_poll_error token=%s error=%s", subject_token, exc)

        time.sleep(poll_interval)

    _log.warning("mail.approval_timeout token=%s", subject_token)
    return None
