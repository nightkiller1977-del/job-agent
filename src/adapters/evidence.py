import hashlib
from datetime import datetime
from typing import List, Dict, Any, Optional
from pathlib import Path

class ApplyEvidence:
    """Represents a structured evidence record of a single job application attempt."""

    def __init__(
        self,
        vendor: str,
        url: str,
        resume_path: Optional[str] = None,
        fields_filled: Optional[List[str]] = None,
        blocker_detected: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        extra_meta: Optional[Dict[str, Any]] = None
    ):
        self.timestamp = datetime.utcnow().isoformat()
        self.vendor = vendor.lower().strip()
        self.url = url
        self.fields_filled = fields_filled or []
        self.blocker_detected = blocker_detected
        self.screenshot_path = screenshot_path
        self.extra_meta = extra_meta or {}

        # Calculate MD5 hash of the submitted resume file if provided and accessible
        self.resume_hash = None
        if resume_path:
            p = Path(resume_path).expanduser()
            if p.exists() and p.is_file():
                try:
                    content = p.read_bytes()
                    self.resume_hash = hashlib.md5(content).hexdigest()
                except Exception:
                    pass

    def to_dict(self) -> Dict[str, Any]:
        """Serializes the evidence record to a dictionary suitable for database extra_json storage."""
        return {
            "evidence_timestamp": self.timestamp,
            "evidence_vendor": self.vendor,
            "evidence_url": self.url,
            "evidence_resume_hash": self.resume_hash,
            "evidence_fields_filled": self.fields_filled,
            "evidence_blocker": self.blocker_detected,
            "evidence_screenshot": self.screenshot_path,
            "evidence_extra": self.extra_meta
        }

class EvidenceBuilder:
    """Builder pattern to construct ApplyEvidence instances incrementally during apply execution."""

    def __init__(self, vendor: str, url: str):
        self.vendor = vendor
        self.url = url
        self.resume_path: Optional[str] = None
        self.fields_filled: List[str] = []
        self.blocker_detected: Optional[str] = None
        self.screenshot_path: Optional[str] = None
        self.extra_meta: Dict[str, Any] = {}

    def with_resume(self, resume_path: str) -> "EvidenceBuilder":
        self.resume_path = resume_path
        return self

    def add_field(self, field_name: str) -> "EvidenceBuilder":
        if field_name and field_name not in self.fields_filled:
            self.fields_filled.append(field_name)
        return self

    def add_fields(self, field_names: List[str]) -> "EvidenceBuilder":
        for fn in field_names:
            if fn:
                self.add_field(fn)
        return self

    def with_blocker(self, blocker: str) -> "EvidenceBuilder":
        self.blocker_detected = blocker
        return self

    def with_screenshot(self, path: str) -> "EvidenceBuilder":
        self.screenshot_path = path
        return self

    def add_meta(self, key: str, value: Any) -> "EvidenceBuilder":
        self.extra_meta[key] = value
        return self

    def build(self) -> ApplyEvidence:
        return ApplyEvidence(
            vendor=self.vendor,
            url=self.url,
            resume_path=self.resume_path,
            fields_filled=self.fields_filled,
            blocker_detected=self.blocker_detected,
            screenshot_path=self.screenshot_path,
            extra_meta=self.extra_meta
        )
