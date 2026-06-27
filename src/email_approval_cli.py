#!/usr/bin/env python3
"""
CLI wrapper around email_helper approval functions.
Called by electron/services/emailApprovalService.js.

Usage:
  python3 email_approval_cli.py send   <to> <subject_token> <body>
  python3 email_approval_cli.py poll   <subject_token> [timeout_seconds]

Exit codes: 0 = success/match found, 1 = timeout/error
Stdout on poll: "approved:N", "denied", or "timeout"
"""
import sys
import os
import pathlib

# Add parent src dir so we can import email_helper
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from email_helper import send_approval_email, poll_for_approval_reply
from dotenv import load_dotenv

# Load .env from job-agent root
_env_path = pathlib.Path(__file__).parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=False)

SEND_EMAIL    = os.environ.get("APPROVAL_SEND_EMAIL", "")
SEND_PASSWORD = os.environ.get("APPROVAL_SEND_PASSWORD", "")
NOTIFY_EMAIL  = os.environ.get("APPROVAL_NOTIFY_EMAIL", "")


def cmd_send(to: str, subject_token: str, body: str) -> None:
    if not SEND_EMAIL or not SEND_PASSWORD:
        print("error: APPROVAL_SEND_EMAIL / APPROVAL_SEND_PASSWORD not set in .env", file=sys.stderr)
        sys.exit(1)
    subject = f"AI Commander Approval [{subject_token}]"
    send_approval_email(SEND_EMAIL, SEND_PASSWORD, to, subject, body)
    print("sent")


def cmd_poll(subject_token: str, timeout: int = 1800) -> None:
    if not SEND_EMAIL or not SEND_PASSWORD:
        print("error: APPROVAL_SEND_EMAIL / APPROVAL_SEND_PASSWORD not set in .env", file=sys.stderr)
        sys.exit(1)
    result = poll_for_approval_reply(SEND_EMAIL, SEND_PASSWORD, subject_token, timeout=timeout)
    if result is None:
        print("timeout")
        sys.exit(1)
    elif result == "denied":
        print("denied")
    else:
        print(f"approved:{result}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "send":
        if len(sys.argv) < 5:
            print("usage: send <to> <subject_token> <body>", file=sys.stderr)
            sys.exit(1)
        cmd_send(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "poll":
        if len(sys.argv) < 3:
            print("usage: poll <subject_token> [timeout_seconds]", file=sys.stderr)
            sys.exit(1)
        timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 1800
        cmd_poll(sys.argv[2], timeout)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
