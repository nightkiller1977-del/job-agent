"""Phase 3 — Multi-Signal Email Confirmation Tracker.

Connects to iCloud Mail via IMAP to detect employer/ATS application receipts,
correlates them with applied jobs using a multi-signal confidence model, and
transitions jobs to confirmed_by_employer in StateManager.

Guardrails:
1. Vendor-Domain Aware: Matches ATS vendor domains (@greenhouse-mail.io, @myworkday.com, etc.)
   and employer domains.
2. Multi-Signal Scoring: Weights company, domain, title, confirmation ID, and timestamp.
3. Message-ID Deduplication: Idempotent processing; never re-processes emails.
4. Privacy: Stores extracted confirmation metadata and IDs, never full raw email bodies.
"""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console

logger = logging.getLogger(__name__)
console = Console()

_PROCESSED_EMAILS_FILE = Path(__file__).resolve().parents[1] / "state" / "processed_emails.json"

# Known ATS Vendor Domains
ATS_VENDOR_DOMAINS = [
    "myworkday.com",
    "myworkdayjobs.com",
    "greenhouse-mail.io",
    "greenhouse.io",
    "boards.greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "icims.com",
    "brassring.com",
    "linkedin.com",
    "indeed.com",
]

# Receipt signal patterns (reused from receipt.py)
CONFIRMATION_SUBJECT_PATTERNS = [
    r"thank you for (applying|your application)",
    r"application (received|submitted|confirmation|confirmed)",
    r"we(?:'|’|)ve received your application",
    r"successfully (applied|submitted)",
    r"your application to",
    r"thanks for applying",
]

REF_ID_PATTERN = re.compile(
    r"(?:confirmation|reference|requisition|application|req|ref)\s*(?:number|id|no\.?|#)\s*[:#]?\s*([a-z0-9][a-z0-9-]{3,})",
    re.IGNORECASE,
)


def _decode_header(hdr: str) -> str:
    if not hdr:
        return ""
    parts = email.header.decode_header(hdr)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded)


class EmailConfirmationTracker:
    def __init__(self, state_manager=None, processed_file: Optional[Path] = None):
        self.state_manager = state_manager
        self.processed_file = processed_file or _PROCESSED_EMAILS_FILE
        self._processed_ids = self._load_processed_ids()

    def _load_processed_ids(self) -> set[str]:
        if self.processed_file.exists():
            try:
                with open(self.processed_file) as f:
                    return set(json.load(f))
            except Exception:
                pass
        return set()

    def _save_processed_ids(self) -> None:
        try:
            self.processed_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.processed_file, "w") as f:
                json.dump(list(self._processed_ids), f, indent=2)
        except Exception as exc:
            logger.warning("Could not save processed email IDs: %s", exc)

    def extract_confirmation_id(self, text: str) -> Optional[str]:
        """Extract application/confirmation/requisition ID from text snippet."""
        if not text:
            return None
        m = REF_ID_PATTERN.search(text)
        return m.group(1) if m else None

    def calculate_match_score(
        self,
        msg_sender: str,
        msg_subject: str,
        msg_body_snippet: str,
        msg_date: Optional[datetime],
        job: Dict[str, Any],
    ) -> Tuple[float, Dict[str, Any]]:
        """Compute multi-signal match score between an email and a job record.

        Weights:
          CompanyMatch: 0.35
          SenderDomainMatch: 0.25
          TitleMatch: 0.20
          ConfirmationIdFound: 0.15
          TimestampProximity: 0.05
        """
        score = 0.0
        evidence: Dict[str, Any] = {}

        sender_clean = msg_sender.lower()
        subject_clean = msg_subject.lower()
        snippet_clean = msg_body_snippet.lower()
        full_text = f"{sender_clean} {subject_clean} {snippet_clean}"

        company = (job.get("company") or "").lower().strip()
        title = (job.get("title") or "").lower().strip()
        job_url = (job.get("url") or "").lower()

        # 1. Company Match (0.35)
        if company and company in full_text:
            score += 0.35
            evidence["company_matched"] = company

        # 2. Sender Domain Match (0.25)
        domain_matched = False
        for vendor_dom in ATS_VENDOR_DOMAINS:
            if vendor_dom in sender_clean or vendor_dom in job_url:
                domain_matched = True
                evidence["vendor_domain"] = vendor_dom
                break

        if not domain_matched and company:
            # Check employer domain
            company_token = re.sub(r"[^\w]", "", company.split()[0])
            if company_token and company_token in sender_clean:
                domain_matched = True
                evidence["employer_domain"] = company_token

        if domain_matched:
            score += 0.25

        # 3. Title Match (0.20)
        if title:
            # Match meaningful keywords from title
            title_words = [w for w in re.split(r"\W+", title) if len(w) > 3]
            matched_words = [w for w in title_words if w in full_text]
            if len(matched_words) >= 2 or (len(title_words) == 1 and matched_words):
                score += 0.20
                evidence["title_matched"] = matched_words

        # 4. Confirmation ID Found (0.15)
        raw_text = f"{msg_subject} {msg_body_snippet}"
        conf_id = self.extract_confirmation_id(raw_text)
        if conf_id:
            score += 0.15
            evidence["confirmation_id"] = conf_id

        # 5. Timestamp Proximity (0.05)
        applied_at_str = job.get("applied_at")
        if applied_at_str and msg_date:
            try:
                applied_date = datetime.fromisoformat(applied_at_str.replace("Z", "+00:00")).replace(tzinfo=None)
                delta = abs((msg_date - applied_date).total_seconds())
                if delta <= 7 * 86400:  # Within 7 days
                    score += 0.05
                    evidence["timestamp_proximity_days"] = round(delta / 86400, 1)
            except Exception:
                pass

        return min(round(score, 2), 1.0), evidence

    def scan_inbox_and_confirm(
        self,
        days: int = 7,
        dry_run: bool = False,
        email_addr: Optional[str] = None,
        password: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Connects to IMAP, scans recent messages, and matches with applied jobs."""
        if not self.state_manager:
            from .state_manager import StateManager
            self.state_manager = StateManager()

        user_email = (
            email_addr
            or os.environ.get("EMAIL_2FA_ADDRESS", "")
            or os.environ.get("USAJOBS_EMAIL", "")
            or "anthonyclarkins@icloud.com"
        )
        imap_pwd = (
            password
            or os.environ.get("ICLOUD_APP_PASSWORD_PERSONAL", "")
            or os.environ.get("ICLOUD_APP_PASSWORD", "")
            or os.environ.get("IMAP_PASSWORD", "")
        )

        if not imap_pwd:
            console.print("[yellow]Email confirmation check skipped: ICLOUD_APP_PASSWORD_PERSONAL / IMAP_PASSWORD not configured.[/yellow]")
            return []

        # Fetch applied jobs from state
        with self.state_manager._connect() as conn:
            applied_jobs = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM jobs WHERE status = 'applied' AND (confirmation_status IS NULL OR confirmation_status != 'confirmed_by_employer')"
                ).fetchall()
            ]

        if not applied_jobs:
            console.print("[dim]No unconfirmed applied jobs pending verification.[/dim]")
            return []

        results = []
        try:
            imap_server = "imap.mail.me.com"
            mail = imaplib.IMAP4_SSL(imap_server, 993)
            mail.login(user_email, imap_pwd)
            mail.select("INBOX", readonly=True)

            since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
            typ, msg_ids = mail.search(None, f'(SINCE "{since_date}")')
            if typ != "OK" or not msg_ids[0]:
                mail.logout()
                return []

            for mid in msg_ids[0].split():
                typ, data = mail.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)] BODY.PEEK[TEXT]<0.1000>)")
                if typ != "OK" or not data:
                    continue

                raw_msg = email.message_from_bytes(data[0][1] if isinstance(data[0], tuple) else data[0])
                msg_id = raw_msg.get("Message-ID", "").strip()
                if msg_id and msg_id in self._processed_ids:
                    continue

                subject = _decode_header(raw_msg.get("Subject", ""))
                sender = _decode_header(raw_msg.get("From", ""))
                date_str = raw_msg.get("Date", "")
                parsed_date = None
                if date_str:
                    try:
                        parsed_date = email.utils.parsedate_to_datetime(date_str).replace(tzinfo=None)
                    except Exception:
                        pass

                body_snippet = ""
                if len(data) > 1 and isinstance(data[1], tuple) and data[1][1]:
                    try:
                        body_snippet = data[1][1].decode("utf-8", errors="ignore")
                    except Exception:
                        pass

                # Check if subject matches confirmation copy
                if not any(re.search(pat, subject, re.IGNORECASE) for pat in CONFIRMATION_SUBJECT_PATTERNS):
                    continue

                # Match against applied jobs
                best_job = None
                best_score = 0.0
                best_evidence = {}

                for job in applied_jobs:
                    score, ev = self.calculate_match_score(sender, subject, body_snippet, parsed_date, job)
                    if score > best_score:
                        best_score = score
                        best_job = job
                        best_evidence = ev

                if best_job and best_score >= 0.50:
                    outcome = {
                        "job_id": best_job["job_id"],
                        "title": best_job.get("title"),
                        "company": best_job.get("company"),
                        "score": best_score,
                        "evidence": best_evidence,
                        "subject": subject,
                        "sender": sender,
                        "confirmed": best_score >= 0.85,
                    }
                    results.append(outcome)

                    if best_score >= 0.85 and not dry_run:
                        # Transition confirmation_status to confirmed_by_employer
                        try:
                            # Advance through intermediate states if starting at None
                            curr_conf = best_job.get("confirmation_status")
                            if curr_conf is None:
                                self.state_manager.transition_confirmation(best_job["job_id"], "submitting")
                                self.state_manager.transition_confirmation(best_job["job_id"], "submitted")
                            self.state_manager.transition_confirmation(best_job["job_id"], "confirmed_by_employer")
                            console.print(f"[green]✓ Application Confirmed by Employer:[/green] {best_job.get('title')} @ {best_job.get('company')} (Score: {best_score})")
                        except Exception as exc:
                            logger.warning("Could not transition confirmation for %s: %s", best_job["job_id"], exc)

                    if msg_id and not dry_run:
                        self._processed_ids.add(msg_id)

            mail.logout()
            if not dry_run:
                self._save_processed_ids()

        except Exception as exc:
            console.print(f"[yellow]IMAP check error (non-fatal): {exc}[/yellow]")

        return results
