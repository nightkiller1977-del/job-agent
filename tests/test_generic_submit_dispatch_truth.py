"""Full-stack submit-dispatch truthfulness regression.

Exercises real GenericAtsAdapter._gated_submit → real submit gate → real
ExternalApplySession outcome handling → real SubmissionLedger in a tmp file.
Only the browser boundary is faked; adapter result, status classification,
and ledger behavior are the code under test.

Companion to tests/test_recovery_submit_dispatch_truth.py, which already
covers the equivalent invariant for the BrowserUseRecoveryRefactored path.
This suite closes the gap for GenericAtsAdapter's primary submit path.

Scaffold — real cases added in a follow-up commit on this branch.
"""
import importlib

import pytest


def test_adapters_import_cleanly():
    """Baseline: the modules under test must import without side effects.

    Guards against a regression where importing an adapter (e.g. via a lazy
    import graph in orchestrator.py) requires a live browser or config file.
    """
    importlib.import_module("src.sources.adapters.generic")
    importlib.import_module("src.sources.adapters.session")
    importlib.import_module("src.sources.adapters.idempotency")
    importlib.import_module("src.sources.adapters.receipt")
