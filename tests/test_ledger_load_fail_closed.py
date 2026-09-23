"""SubmissionLedger._load() must distinguish a legitimately absent first-run
file from an existing file that can't be parsed. Treating a corrupt/unreadable
ledger as empty history would let a duplicate-application check silently pass
and re-submit a job that was already applied to, or already unresolved.
"""
import pytest

from src.sources.adapters.idempotency import SubmissionLedger, LedgerUnreadableError


def test_missing_file_is_empty_history(tmp_path):
    ledger = SubmissionLedger(path=tmp_path / "apply_ledger.json")
    assert ledger.record("vendor|https://example.com/job/1") is None
    assert ledger.already_applied("vendor|https://example.com/job/1") is False


def test_corrupt_json_raises_instead_of_empty(tmp_path):
    path = tmp_path / "apply_ledger.json"
    path.write_text("{not valid json")
    ledger = SubmissionLedger(path=path)
    with pytest.raises(LedgerUnreadableError):
        ledger.record("vendor|https://example.com/job/1")


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"vendor|https://example.com/job/1":{"phase":"unknown"}}',
        '{"vendor|https://example.com/job/1":{"phase":"receipt_verified","job_id":"job-1"}}',
        '{"vendor|https://example.com/job/1":{"phase":"receipt_verified","attempt_id":" ","ts":1}}',
        '{"vendor|https://example.com/job/1":{"phase":"receipt_verified","attempt_id":"attempt-1","ts":true}}',
        '{"vendor|https://example.com/job/1":{"phase":"receipt_verified","attempt_id":"attempt-1","ts":"now"}}',
    ],
)
def test_structurally_invalid_ledger_raises(tmp_path, payload):
    path = tmp_path / "apply_ledger.json"
    path.write_text(payload)

    with pytest.raises(LedgerUnreadableError):
        SubmissionLedger(path=path).validate()


@pytest.mark.asyncio
async def test_corrupt_ledger_blocks_session_pre_flight(tmp_path):
    """The session's duplicate/in-progress gate must fail closed and return a
    blocked result, not proceed (or crash) as if there were no prior
    submission history."""
    from unittest.mock import MagicMock
    from src.sources.adapters.session import ExternalApplySession

    path = tmp_path / "apply_ledger.json"
    path.write_text("{not valid json")

    session = ExternalApplySession.__new__(ExternalApplySession)
    session.ledger = SubmissionLedger(path=path)
    session.run_log = MagicMock()

    result = await session.apply({"url": "https://example.com/job/1"}, auto_submit=True)

    assert result.status == "ledger_unreadable"
    assert result.submitted is False


@pytest.mark.asyncio
async def test_corrupt_ledger_blocks_legacy_flow_before_browser(
    tmp_path, monkeypatch
):
    """The legacy Jobright path must validate durable history pre-launch."""
    from src.sources.jobright import JobrightScraper

    path = tmp_path / "apply_ledger.json"
    path.write_text("{not valid json")
    scraper = JobrightScraper.__new__(JobrightScraper)
    scraper.config = {}
    scraper._submission_ledger = SubmissionLedger(path=path)
    browser_starts = 0

    async def _start_browser(**_kwargs):
        nonlocal browser_starts
        browser_starts += 1
        raise AssertionError("browser must not start with an unreadable ledger")

    scraper._start_browser = _start_browser
    monkeypatch.setenv("USE_ADAPTER_REGISTRY", "0")

    submitted = await scraper.apply_external_ats_job(
        {"job_id": "job-1", "title": "Engineer", "company": "Acme"},
        "https://boards.greenhouse.io/acme/jobs/1",
        auto_submit=True,
    )

    assert submitted is False
    assert scraper.last_apply_status == "ledger_unreadable"
    assert browser_starts == 0


@pytest.mark.asyncio
async def test_corrupt_ledger_blocks_native_jobright_before_browser(tmp_path):
    """Native jobright.ai applies share the same pre-browser ledger gate."""
    from src.sources.jobright import JobrightScraper

    path = tmp_path / "apply_ledger.json"
    path.write_text("{not valid json")
    scraper = JobrightScraper.__new__(JobrightScraper)
    scraper.config = {}
    scraper._submission_ledger = SubmissionLedger(path=path)
    browser_starts = 0

    async def _start_browser(**_kwargs):
        nonlocal browser_starts
        browser_starts += 1
        raise AssertionError("browser must not start with an unreadable ledger")

    scraper._start_browser = _start_browser
    submitted = await scraper.apply(
        {
            "job_id": "job-native",
            "title": "Engineer",
            "company": "Acme",
            "url": "https://jobright.ai/jobs/info/native",
        },
        auto_submit=True,
    )

    assert submitted is False
    assert scraper.last_apply_status == "ledger_unreadable"
    assert browser_starts == 0
