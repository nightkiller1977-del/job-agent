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

# Env names an IMAP *app-specific* password may be stored under. IMAP_PASSWORD is the
# canonical name; ICLOUD_APP_PASSWORD_ICLOUD / _MAC are what the central aicc-secrets
# store uses for the two iCloud accounts (the icloud-mail MCP reads the same names),
# so one rotation there is picked up here without renaming anything.
#
# The *site* login password (USAJOBS_PASSWORD etc.) is deliberately absent: iCloud,
# Gmail and Yahoo reject account passwords over IMAP outright, so falling back to it
# — which the old code did — can only ever produce AUTHENTICATIONFAILED, and then
# burned the whole 2FA window retrying that same doomed login every 10 s (ACES-283).
_IMAP_ADDRESS_KEYS = ("EMAIL_2FA_ADDRESS", "USAJOBS_EMAIL", "IMAP_USER")
_IMAP_PASSWORD_KEYS_GENERIC = (
    "IMAP_PASSWORD",
    "ICLOUD_APP_PASSWORD_PERSONAL",
    "ICLOUD_APP_PASSWORD",
)
_IMAP_PASSWORD_KEYS_BY_DOMAIN = {
    "@icloud.com": ("ICLOUD_APP_PASSWORD_ICLOUD", "ICLOUD_APP_PASSWORD_MAC"),
    "@me.com":     ("ICLOUD_APP_PASSWORD_MAC", "ICLOUD_APP_PASSWORD_ICLOUD"),
    "@mac.com":    ("ICLOUD_APP_PASSWORD_MAC", "ICLOUD_APP_PASSWORD_ICLOUD"),
}

# Substrings (upper-cased) that identify a rejected-credential IMAP response, as
# opposed to a transient connection/server error worth retrying.
_IMAP_AUTH_FAILURE_MARKERS = (
    "AUTHENTICATIONFAILED",
    "AUTHENTICATE FAILED",
    "LOGIN FAILED",
    "INVALID CREDENTIALS",
)


def imap_password_candidates(email_addr: str) -> tuple[str, ...]:
    """Env var names to try, in order, for *email_addr*'s IMAP app password."""
    keys = list(_IMAP_PASSWORD_KEYS_GENERIC)
    addr = (email_addr or "").lower()
    for domain, names in _IMAP_PASSWORD_KEYS_BY_DOMAIN.items():
        if domain in addr:
            keys.extend(n for n in names if n not in keys)
            break
    return tuple(keys)


def resolve_imap_credentials(email_addr: str = "", password: str = "") -> tuple[str, str]:
    """Return ``(address, app_password)`` for IMAP access, or ``("", "")`` parts when
    unresolved. Explicit arguments win; otherwise the address comes from
    EMAIL_2FA_ADDRESS / USAJOBS_EMAIL / IMAP_USER and the password from
    :func:`imap_password_candidates`. Shared by the USAJobs email-2FA reader and the
    employer-confirmation tracker so both accept the same key names and neither can
    drift back to a site-login password."""
    import os
    addr = (email_addr or "").strip()
    if not addr:
        for key in _IMAP_ADDRESS_KEYS:
            val = (os.environ.get(key) or "").strip()
            if val:
                addr = val
                break
    pwd = (password or "").strip()
    if not pwd and addr:
        for key in imap_password_candidates(addr):
            val = (os.environ.get(key) or "").strip()
            if val:
                pwd = val
                break
    return addr, pwd


def is_imap_auth_failure(exc: BaseException) -> bool:
    """True when *exc* is the server rejecting the credentials (not a transient error)."""
    text = str(exc).upper()
    return any(marker in text for marker in _IMAP_AUTH_FAILURE_MARKERS)


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
            try:
                mail.login(email_address, password)
            except imaplib.IMAP4.error as login_exc:
                if is_imap_auth_failure(login_exc):
                    # Rejected credentials will not fix themselves inside this window:
                    # fail fast with one actionable line instead of retrying the same
                    # doomed login every 10 s until the 2FA timeout (ACES-283).
                    _log.warning(
                        "mail.2fa_auth_failed imap=%s — IMAP rejected the app-specific "
                        "password; rotate it (IMAP_PASSWORD / ICLOUD_APP_PASSWORD_*, see "
                        "SECRETS.md). Not retrying this run.",
                        imap_server,
                    )
                    try:
                        mail.logout()
                    except Exception:
                        pass
                    return None
                raise
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
