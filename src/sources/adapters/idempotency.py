"""Phase 0.2 — idempotent submission ledger.

Prevents a retry, a recovery pass, or a crash between click and receipt from sending
a duplicate application. Keyed by a canonical (vendor, normalized-URL) key so the
same posting reached via two different tracking URLs is still recognised as one.

Lifecycle per attempt:
    claim(key, attempt_id)  -> atomically writes "submit_in_progress" iff clear
    complete(key, attempt_id, verified) -> "receipt_verified" | "submission_unverified"

Reads before launching a browser:
    already_applied(key)  -> a prior attempt reached receipt_verified  -> skip, do not resubmit
    in_progress(key)      -> a prior attempt died mid-submit           -> do not blindly resubmit

Backed by a small JSON file under state/. Read-modify-write transitions use an OS
file lock so independent scheduler processes cannot both claim the same posting.
"""
from __future__ import annotations

import json
import fcntl
import os
import tempfile
import time
from contextlib import contextmanager
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


class LedgerUnreadableError(Exception):
    """The ledger file exists but could not be parsed (corrupt JSON, bad
    permissions, etc). Distinct from a first-run missing file: history that
    can't be read must never be treated as empty history, or a prior
    unresolved/verified submission could be silently forgotten."""


class LedgerOwnershipError(RuntimeError):
    """A stale attempt tried to mutate a key now owned by another attempt."""

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
                raw = f.read()
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise LedgerUnreadableError(f"ledger at {self.path} could not be read: {e}") from e
        try:
            return json.loads(raw)
        except Exception as e:
            raise LedgerUnreadableError(f"ledger at {self.path} is corrupt: {e}") from e

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

    @contextmanager
    def _exclusive_lock(self):
        """Serialize read-modify-write transitions across worker processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        try:
            with open(lock_path, "a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise LedgerUnreadableError(
                f"ledger lock at {lock_path} could not be acquired: {exc}"
            ) from exc

    # ---- queries -------------------------------------------------------------
    def record(self, key: str) -> dict | None:
        if not key:
            return None
        return self._load().get(key)

    def validate(self) -> None:
        """Verify durable history is readable and its lock is available.

        A missing first-run file is valid empty history.  Existing corrupt or
        inaccessible state must fail before an employer-facing browser starts.
        """
        with self._exclusive_lock():
            self._load()

    def record_for_job(self, job_id: str) -> tuple[str, dict] | None:
        """Return the newest durable record associated with *job_id*.

        The ATS key can differ from the discovery URL stored on the job row.
        Keeping the local job identifier on the pre-submit claim lets startup
        recovery find the receipt even if the process crashed before it could
        persist the resolved ATS URL back to SQLite.
        """
        if not job_id:
            return None
        matches = [
            (key, record)
            for key, record in self._load().items()
            if isinstance(record, dict) and str(record.get("job_id") or "") == job_id
        ]
        if not matches:
            return None
        key, record = max(matches, key=lambda item: float(item[1].get("ts", 0)))
        return key, dict(record)

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

    def clear(self, key: str) -> None:
        """Drop a marker entirely — used when an attempt ended WITHOUT a submit
        click (e.g. a login wall or blocker), so the in-progress marker must not
        linger as unverified and block future attempts."""
        if not key:
            return
        with self._exclusive_lock():
            data = self._load()
            if key in data:
                del data[key]
                self._save(data)

    def is_stale_in_progress(self, key: str) -> bool:
        rec = self.record(key)
        if not rec or rec.get("phase") != PHASE_IN_PROGRESS:
            return False
        return (time.time() - float(rec.get("ts", 0))) > STALE_AFTER_S

    # ---- transitions ---------------------------------------------------------
    def claim(self, key: str, attempt_id: str, *, job_id: str = "") -> dict | None:
        """Atomically claim *key* for one submit attempt.

        Returns ``None`` when this caller wrote the in-progress marker. If any
        prior marker already exists, returns that record without modifying it.
        The compare-and-set and write share one OS file lock, so concurrent
        processes cannot both pass the duplicate gate.
        """
        if not key:
            return None
        with self._exclusive_lock():
            data = self._load()
            existing = data.get(key)
            if existing is not None:
                return dict(existing)
            record = {
                "phase": PHASE_IN_PROGRESS,
                "attempt_id": attempt_id,
                "ts": time.time(),
            }
            if job_id:
                record["job_id"] = job_id
            data[key] = record
            self._save(data)
        return None

    def begin(self, key: str, attempt_id: str) -> None:
        if not key:
            return
        with self._exclusive_lock():
            data = self._load()
            data[key] = {"phase": PHASE_IN_PROGRESS, "attempt_id": attempt_id, "ts": time.time()}
            self._save(data)

    def complete(self, key: str, attempt_id: str, verified: bool) -> None:
        if not key:
            return
        with self._exclusive_lock():
            data = self._load()
            existing = data.get(key) if isinstance(data.get(key), dict) else {}
            existing_attempt_id = str(existing.get("attempt_id") or "")
            if existing and existing_attempt_id != str(attempt_id):
                raise LedgerOwnershipError(
                    f"submission key {key} belongs to attempt "
                    f"{existing_attempt_id or '(unknown)'}, not {attempt_id}"
                )
            record = {
                "phase": PHASE_VERIFIED if verified else PHASE_UNVERIFIED,
                "attempt_id": attempt_id,
                "ts": time.time(),
            }
            if existing.get("job_id"):
                record["job_id"] = existing["job_id"]
            data[key] = record
            self._save(data)
