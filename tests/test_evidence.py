import pytest
from pathlib import Path
from src.adapters.evidence import ApplyEvidence, EvidenceBuilder

def test_evidence_builder_and_serialization(tmp_path):
    # Setup dummy resume file
    resume = tmp_path / "my_resume.pdf"
    resume.write_bytes(b"Dummy PDF Content")
    
    # Build evidence
    builder = EvidenceBuilder(vendor="greenhouse", url="https://boards.greenhouse.io/job/123")
    builder.with_resume(str(resume))
    builder.add_field("first_name")
    builder.add_fields(["last_name", "email", "phone"])
    builder.with_blocker("stuck on captcha")
    builder.with_screenshot("/tmp/screenshot.png")
    builder.add_meta("attempt_no", 2)
    
    evidence = builder.build()
    data = evidence.to_dict()
    
    # Assert fields are correctly formatted
    assert data["evidence_vendor"] == "greenhouse"
    assert data["evidence_url"] == "https://boards.greenhouse.io/job/123"
    assert len(data["evidence_fields_filled"]) == 4
    assert "email" in data["evidence_fields_filled"]
    assert data["evidence_blocker"] == "stuck on captcha"
    assert data["evidence_screenshot"] == "/tmp/screenshot.png"
    assert data["evidence_extra"] == {"attempt_no": 2}
    
    # MD5 hash calculation check
    import hashlib
    expected_hash = hashlib.md5(b"Dummy PDF Content").hexdigest()
    assert data["evidence_resume_hash"] == expected_hash
    
    # Timestamp presence check
    assert "evidence_timestamp" in data
    assert isinstance(data["evidence_timestamp"], str)
