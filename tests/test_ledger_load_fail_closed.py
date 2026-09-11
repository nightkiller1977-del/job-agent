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
