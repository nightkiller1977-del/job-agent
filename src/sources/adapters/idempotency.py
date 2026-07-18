"""Phase 0.2 — idempotent submission ledger.

Prevents a retry, a recovery pass, or a crash between click and receipt from sending
a duplicate application. Keyed by a canonical (vendor, normalized-URL) key so the
same posting reached via two different tracking URLs is still recognised as one.

Lifecycle per attempt:
    begin(key, attempt_id)  -> writes a "submit_in_progress" marker BEFORE the click
    complete(key, attempt_id, verified) -> "receipt_verified" | "submission_unverified"

Reads before launching a browser:
    already_applied(key)  -> a prior attempt reached receipt_verified  -> skip, do not resubmit
    in_progress(key)      -> a prior attempt died mid-submit           -> do not blindly resubmit

Backed by a small JSON file under state/ so it is self-contained and does not touch
the concurrently-edited state_manager.py. Full jobs.db integration is a later step.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .generic import detect_vendor

try:
    from ...url_utils import normalize_external_url
except Exception:  # pragma: no cover - url_utils is expected to exist
    def normalize_external_url(url: str) -> str:  # type: ignore
        return (url or "").strip().lower()

_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "state" / "apply_ledger.json"

PHASE_IN_PROGRESS = "submit_in_progress"
PHASE_VERIFIED = "receipt_verified"
PHASE_UNVERIFIED = "submission_unverified"

# An in-progress marker older than this (seconds) is treated as a crashed attempt,
# not a live one — it still blocks a *blind* resubmit but is reported as stale.
STALE_AFTER_S = 6 * 60 * 60


def canonical_key(job: dict) -> str:
    """Stable dedupe key for a posting: '<vendor>|<normalized-url>'."""
    url = (job.get("url") or job.get("external_url") or "") if isinstance(job, dict) else ""
    norm = normalize_external_url(url)
    vendor = detect_vendor(norm or url)
    return f"{vendor}|{norm}" if norm else ""


class SubmissionLedger:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else _DEFAULT_PATH

    # ---- io ------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # atomic write so a crash never leaves a half-written ledger
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- queries -------------------------------------------------------------
    def record(self, key: str) -> dict | None:
        if not key:
            return None
        return self._load().get(key)

    def already_applied(self, key: str) -> bool:
        rec = self.record(key)
        return bool(rec and rec.get("phase") == PHASE_VERIFIED)

    def in_progress(self, key: str) -> bool:
        rec = self.record(key)
        return bool(rec and rec.get("phase") == PHASE_IN_PROGRESS)

    def needs_reconciliation(self, key: str) -> bool:
        """A prior attempt clicked submit but the receipt could not be verified.
        We must not blindly resubmit — it may or may not have gone through."""
        rec = self.record(key)
        return bool(rec and rec.get("phase") == PHASE_UNVERIFIED)

    def is_stale_in_progress(self, key: str) -> bool:
        rec = self.record(key)
        if not rec or rec.get("phase") != PHASE_IN_PROGRESS:
            return False
        return (time.time() - float(rec.get("ts", 0))) > STALE_AFTER_S

    # ---- transitions ---------------------------------------------------------
    def begin(self, key: str, attempt_id: str) -> None:
        if not key:
            return
        data = self._load()
        data[key] = {"phase": PHASE_IN_PROGRESS, "attempt_id": attempt_id, "ts": time.time()}
        self._save(data)

    def complete(self, key: str, attempt_id: str, verified: bool) -> None:
        if not key:
            return
        data = self._load()
        data[key] = {
            "phase": PHASE_VERIFIED if verified else PHASE_UNVERIFIED,
            "attempt_id": attempt_id,
            "ts": time.time(),
        }
        self._save(data)
