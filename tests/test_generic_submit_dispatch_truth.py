"""Full-stack submit-dispatch truthfulness regression.

Exercises real GenericAtsAdapter → real ExternalApplySession outcome handling
→ real SubmissionLedger in a tmp file. Only the browser boundary is faked
(_start_browser, _close_browser, _profile_dir). Adapter logic, submit gate,
result classification, ledger transitions, and cross-process persistence are
the code under test.

Companion to tests/test_recovery_submit_dispatch_truth.py, which covers the
equivalent invariant for the BrowserUseRecoveryRefactored path. This suite
closes the gap for GenericAtsAdapter's primary submit path.

The critical scenario is (b): a click that DISPATCHES before failing. The
current GenericAtsAdapter._submit swallows every click exception and reports
`submit_not_found`, which flows straight into the session's recovery path
(session.py:302) — recovery may then click submit AGAIN, silently doubling
the possible submission. That is the duplicate-submission risk under test.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.events import RunLog
from src.sources.adapters.context import AtsApplyContext, AtsApplyResult
from src.sources.adapters.generic import GenericAtsAdapter
from src.sources.adapters.idempotency import (
    PHASE_IN_PROGRESS,
    PHASE_UNVERIFIED,
    PHASE_VERIFIED,
    SubmissionLedger,
    canonical_key,
)
from src.sources.adapters.policy import AutoSubmitPolicy, DenyAllPolicy
from src.sources.adapters.registry import AtsAdapterRegistry
from src.sources.adapters.session import ExternalApplySession


# --------------------------------------------------------------------------- #
# CI-config guard
# --------------------------------------------------------------------------- #
def test_adapters_import_cleanly():
    importlib.import_module("src.sources.adapters.generic")
    importlib.import_module("src.sources.adapters.session")
    importlib.import_module("src.sources.adapters.idempotency")
    importlib.import_module("src.sources.adapters.receipt")


# --------------------------------------------------------------------------- #
# fakes — only the BROWSER BOUNDARY is faked; adapter/session/ledger are real
# --------------------------------------------------------------------------- #
class _FakeElement:
    """Fake DOM element with configurable click behavior + a call recorder.

    `mode` selects the click behavior:
      "ok"                 — click resolves cleanly (return True)
      "raise_after_dispatch" — click DISPATCHED at network level, then raised
                              (Playwright TimeoutError during post-click wait).
                              This is the exact bug shape the suite exists to
                              cover: the click may have hit the server.
      "raise_before_dispatch" — provable pre-dispatch failure (element found
                                but click() throws before reaching the network,
                                e.g. detached from DOM). No submission risk.
    """
    def __init__(self, mode: str, counter: dict):
        self.mode = mode
        self.counter = counter  # shared dict {"clicks": int, "dispatched": int}

    async def click(self):
        self.counter["clicks"] += 1
        if self.mode == "ok":
            self.counter["dispatched"] += 1
            return None
        if self.mode == "raise_after_dispatch":
            # Simulate: the network request went out, then the post-click
            # navigation wait timed out. From _submit's perspective, click()
            # raised — but the possible submission is already in flight.
            self.counter["dispatched"] += 1
            raise TimeoutError("Locator.click: Timeout 30000ms exceeded during post-click wait")
        if self.mode == "raise_before_dispatch":
            # Element was found but detached from DOM before the click packet
            # left; no server contact.
            raise RuntimeError("Element is not attached to the DOM")
        raise ValueError(f"unknown click mode: {self.mode}")

    async def fill(self, value):
        return None


class _FakePage:
    """Minimal Playwright-shaped page. Configurable per test.

    Models a realistic pre/post-click state transition: `receipt_signal`
    represents an acceptance panel that appears AFTER the submit click has
    dispatched, not before. This lets verify_receipt(page, baseline=...)
    (which captures baseline BEFORE the click and re-checks after) see the
    correct sequence: no receipt at baseline capture, receipt after dispatch.

    Also handles the new receipt.py auxiliary JS calls:
      - _COUNT_JS (occurrence counting) → 1 iff receipt currently visible
      - _STORE_COUNT_JS (dataset write)  → recorded in `stored_count`
      - _READ_COUNT_JS (dataset read)    → returns stored_count
    """
    def __init__(
        self,
        url: str = "https://form.local/apply",
        submit_click_mode: str = "ok",
        submit_selector_present: bool = True,
        receipt_signal: str | None = None,
    ):
        self.url = url
        self._title = "Apply"
        self.submit_click_mode = submit_click_mode
        self.submit_selector_present = submit_selector_present
        # The receipt this page will show ONCE a click has been dispatched.
        # Before dispatch, the page has no acceptance evidence (realistic —
        # the form is not itself a receipt).
        self._post_dispatch_receipt = receipt_signal
        # Simulated `document.body.dataset.receiptBaselineCount` — verify_receipt
        # writes to it on baseline capture and reads it on post-submit.
        self.stored_count: int = 0
        self.counter = {"clicks": 0, "dispatched": 0, "goto": 0, "evaluate": 0}
        self.frames = []

    @property
    def receipt_signal(self) -> str | None:
        """The signal `_RECEIPT_JS` would return on this page right now.
        None before the first dispatch; `_post_dispatch_receipt` after."""
        if self.counter["dispatched"] == 0:
            return None
        return self._post_dispatch_receipt

    def _current_match_count(self) -> int:
        """The count `_COUNT_JS` would return right now."""
        return 1 if self.receipt_signal else 0

    async def title(self):
        return self._title

    async def goto(self, url, **kw):
        self.counter["goto"] += 1
        self.url = url
        return SimpleNamespace(status=200)

    async def query_selector(self, sel):
        if not self.submit_selector_present:
            return None
        selector_hints = ("submit", "type=\"submit\"", "type='submit'", "apply", "button")
        if any(h in sel.lower() for h in selector_hints):
            return _FakeElement(self.submit_click_mode, self.counter)
        return None

    async def evaluate(self, js, *args):
        self.counter["evaluate"] += 1
        # _detect_blocker: no blocker
        if "captcha" in js and "iframe" in js:
            return None
        # _answer_questions: no labels found
        if "querySelectorAll('label')" in js:
            return []
        # _COUNT_JS carries a sentinel comment identifying it (see receipt.py).
        if "sentinel: acceptance-count harness" in js:
            return self._current_match_count()
        # _STORE_COUNT_JS: caller passes the count as the first arg.
        if "receiptBaselineCount = String" in js:
            if args:
                self.stored_count = int(args[0] or 0)
            return None
        # _READ_COUNT_JS: return the recorded baseline count.
        if "parseInt(document.body.dataset.receiptBaselineCount" in js:
            return self.stored_count
        # _RECEIPT_JS: the primary matcher — returns signal iff receipt is
        # currently visible on the page (post-dispatch). Detected via the
        # sentinel comment inside the production JS.
        if "sentinel: acceptance-matcher harness" in js:
            return self.receipt_signal
        return None

    async def set_input_files(self, selector, path):
        return None


class _CountingFakeRecovery:
    """Stand-in for BrowserUseRecoveryRefactored — records if invoked and can
    optionally re-drive a submit click.

    The point: if the FIRST submit raised-after-dispatch and was misclassified
    as `submit_not_found`, the session triggers recovery (session.py:302). A
    recovery pass that clicks submit again would double the submission — the
    exact defect this suite exists to expose.

    `outcome`:
      "unverified" — recovery re-clicks (if redispatch=True) then returns
                     AtsApplyResult.unverified(). Session adopts this per
                     session.py:318 (adopts unverified from recovery).
      "blocked"    — recovery ALSO fails to find the submit control and
                     returns AtsApplyResult.blocked("submit_not_found").
                     Session does NOT adopt (only adopts submitted/unverified),
                     so the original res.status stands.
    """
    def __init__(self, redispatch: bool = True, outcome: str = "unverified"):
        self.calls = 0
        self.redispatch = redispatch
        self.outcome = outcome

    async def apply(self, ctx: AtsApplyContext) -> AtsApplyResult:
        self.calls += 1
        if self.redispatch:
            # Simulate a naive recovery re-clicking the same submit button —
            # which is exactly what the current codebase is at risk of doing
            # when submit_not_found is (mis)reported despite a real dispatch.
            page = ctx.page
            el = await page.query_selector("button[type=submit]")
            if el:
                try:
                    await el.click()
                except Exception:
                    pass
        if self.outcome == "blocked":
            return AtsApplyResult.blocked(
                "submit_not_found",
                "recovery: also could not find a submit control",
                vendor="generic",
            )
        return AtsApplyResult.unverified(
            "recovery: submit re-clicked; no receipt confirmation observed",
            vendor="generic",
        )


# --------------------------------------------------------------------------- #
# session harness — real ExternalApplySession with a controlled fake page
# --------------------------------------------------------------------------- #
def _make_session(tmp_path, page: _FakePage, monkeypatch, ledger_path=None):
    """Wire a real ExternalApplySession to the fake browser boundary."""
    reg = AtsAdapterRegistry(fallback=GenericAtsAdapter())
    ledger = SubmissionLedger(ledger_path or (tmp_path / "ledger.json"))
    sess = ExternalApplySession(
        {}, registry=reg, ledger=ledger,
        run_log=RunLog(agent="test", runs_dir=tmp_path / "runs"),
    )
    sess._closed = False

    async def _start(load_extensions=False, disable_extensions=False):
        return page

    async def _close(save_session=True):
        sess._closed = True

    monkeypatch.setattr(sess, "_start_browser", _start)
    monkeypatch.setattr(sess, "_close_browser", _close)
    prof = tmp_path / "jobright_profile"
    prof.mkdir(exist_ok=True)
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))
    return sess, ledger


JOB_URL = "https://boards.greenhouse.io/acme/jobs/1"
JOB = {"url": JOB_URL, "job_id": "job-1", "source": "external"}


# --------------------------------------------------------------------------- #
# Scenario A — submit target absent → pre-submit failure, ledger untouched
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_submit_target_absent_records_pre_submit_failure_and_no_click(tmp_path, monkeypatch):
    """A demonstrably-absent submit control must produce `submit_not_found`
    (the legitimate pre-submit failure) with zero click dispatches, and the
    ledger marker must be cleared — the job stays retryable, not frozen."""
    # Recovery also can't find the submit control — returns blocked, which
    # the session does NOT adopt (session.py:318 only adopts submitted or
    # submission_unverified). So the original submit_not_found stands.
    from src.sources.adapters import recovery_browseruse_refactored as rec_mod
    monkeypatch.setattr(rec_mod, "BrowserUseRecoveryRefactored",
                        lambda *a, **kw: _CountingFakeRecovery(redispatch=False,
                                                                outcome="blocked"))

    page = _FakePage(url=JOB_URL, submit_selector_present=False)
    sess, ledger = _make_session(tmp_path, page, monkeypatch)

    res = await sess.apply(JOB, auto_submit=True)

    assert res.status == "submit_not_found", (
        f"absent submit control must report submit_not_found; got {res.status!r}"
    )
    assert page.counter["clicks"] == 0, (
        f"no click should have been dispatched; got clicks={page.counter['clicks']}"
    )
    assert page.counter["dispatched"] == 0
    # Ledger marker cleared — job stays retryable (not frozen at unverified).
    key = canonical_key(JOB)
    rec = ledger.record(key)
    assert rec is None or rec.get("phase") != PHASE_UNVERIFIED, (
        f"absent-submit outcome must not leave PHASE_UNVERIFIED on the ledger; "
        f"got {rec!r}"
    )


# --------------------------------------------------------------------------- #
# Scenario B — click DISPATCHES then raises, no receipt → must NOT re-click
# (the critical duplicate-submission regression; currently RED)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_click_dispatched_then_timeout_no_receipt_preserves_uncertainty(tmp_path, monkeypatch):
    """The click reached the browser (dispatched=1) and then raised TimeoutError.
    Correct behavior: outcome is `submission_unverified`, ledger records
    PHASE_UNVERIFIED, and NO further submit clicks are dispatched (recovery must
    not fire on an ambiguous submit).

    Baseline (current code): _submit swallows the exception → submit_not_found →
    session.py:302 triggers recovery → recovery re-dispatches → total dispatched
    goes to 2. That is the exact duplicate-submission defect."""
    from src.sources.adapters import recovery_browseruse_refactored as rec_mod
    fake_recovery = _CountingFakeRecovery(redispatch=True)
    monkeypatch.setattr(rec_mod, "BrowserUseRecoveryRefactored",
                        lambda *a, **kw: fake_recovery)

    page = _FakePage(url=JOB_URL, submit_click_mode="raise_after_dispatch",
                     receipt_signal=None)
    sess, ledger = _make_session(tmp_path, page, monkeypatch)

    res = await sess.apply(JOB, auto_submit=True)

    # 1. Outcome must be ambiguous, not misclassified as pre-submit failure.
    assert res.status == "submission_unverified", (
        f"dispatched-then-timeout must report submission_unverified; got {res.status!r}. "
        f"This is the primary defect: current _submit returns False on any click "
        f"exception, so the session sees submit_not_found and triggers recovery."
    )

    # 2. No re-dispatch — recovery must not fire for ambiguous submits.
    assert page.counter["dispatched"] == 1, (
        f"exactly one dispatch expected; got dispatched={page.counter['dispatched']}. "
        f"A second dispatch means recovery re-clicked a possibly-succeeded submit "
        f"= duplicate application risk."
    )
    assert fake_recovery.calls == 0, (
        f"recovery must not be invoked on submission_unverified; got calls={fake_recovery.calls}"
    )

    # 3. Ledger records PHASE_UNVERIFIED so reconciliation blocks the next attempt.
    rec = ledger.record(canonical_key(JOB))
    assert rec is not None and rec.get("phase") == PHASE_UNVERIFIED, (
        f"ledger must persist PHASE_UNVERIFIED for the reconciliation hold; got {rec!r}"
    )


# --------------------------------------------------------------------------- #
# Scenario C — click times out but acceptance arrives → verified, no re-click
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_click_dispatched_then_timeout_with_receipt_verifies_without_reclick(tmp_path, monkeypatch):
    """After a dispatched-then-timeout click, if verify_receipt observes valid
    acceptance copy in the DOM, the outcome is verified success — WITHOUT any
    second click. The receipt evidence is what upgrades ambiguity to success."""
    from src.sources.adapters import recovery_browseruse_refactored as rec_mod
    fake_recovery = _CountingFakeRecovery(redispatch=True)
    monkeypatch.setattr(rec_mod, "BrowserUseRecoveryRefactored",
                        lambda *a, **kw: fake_recovery)

    page = _FakePage(
        url=JOB_URL,
        submit_click_mode="raise_after_dispatch",
        receipt_signal="t:application submitted",
    )
    sess, ledger = _make_session(tmp_path, page, monkeypatch)

    res = await sess.apply(JOB, auto_submit=True)

    assert res.status == "applied" and res.verified is True, (
        f"dispatched click + verified receipt must yield applied+verified; "
        f"got status={res.status!r} verified={res.verified}"
    )
    assert page.counter["dispatched"] == 1, (
        f"no re-dispatch — receipt evidence upgrades the single click; "
        f"got dispatched={page.counter['dispatched']}"
    )
    assert fake_recovery.calls == 0
    rec = ledger.record(canonical_key(JOB))
    assert rec is not None and rec.get("phase") == PHASE_VERIFIED, (
        f"ledger must persist PHASE_VERIFIED; got {rec!r}"
    )


# --------------------------------------------------------------------------- #
# Scenario D — prior unresolved ledger → next attempt refuses to click
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prior_unverified_ledger_blocks_next_attempt_before_click(tmp_path, monkeypatch):
    """If the ledger already has PHASE_UNVERIFIED for this job, a fresh apply
    attempt must be refused BEFORE any browser work — no browser launch, no
    click dispatch, ledger unchanged."""
    from src.sources.adapters import recovery_browseruse_refactored as rec_mod
    monkeypatch.setattr(rec_mod, "BrowserUseRecoveryRefactored",
                        lambda *a, **kw: _CountingFakeRecovery(redispatch=False))

    # Seed the ledger BEFORE the session runs.
    key = canonical_key(JOB)
    ledger = SubmissionLedger(tmp_path / "ledger.json")
    ledger.complete(key, "prior-attempt-id", verified=False)
    seeded_ts = ledger.record(key).get("ts")

    page = _FakePage(url=JOB_URL, submit_click_mode="ok")
    # Reuse the seeded ledger by pointing the session at the same path.
    sess, _ = _make_session(tmp_path, page, monkeypatch,
                            ledger_path=tmp_path / "ledger.json")

    res = await sess.apply(JOB, auto_submit=True)

    assert res.status == "submit_unverified_unresolved", (
        f"prior PHASE_UNVERIFIED must block resubmit before clicking; "
        f"got status={res.status!r}"
    )
    assert page.counter["clicks"] == 0 and page.counter["dispatched"] == 0, (
        f"no click of any kind on a reconciliation hold; got counters={page.counter}"
    )
    assert page.counter["goto"] == 0, (
        f"browser navigation must not happen either; got goto={page.counter['goto']}"
    )
    # Ledger untouched — seeded timestamp preserved.
    rec = ledger.record(key)
    assert rec.get("phase") == PHASE_UNVERIFIED and rec.get("ts") == seeded_ts


# --------------------------------------------------------------------------- #
# Scenario E — policy denies → zero submit clicks
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_policy_deny_produces_zero_submit_clicks(tmp_path, monkeypatch):
    """A DenyAllPolicy withholds authorization even under auto_submit=True.
    No click may dispatch; outcome is submit_denied_by_policy; ledger clears
    any in-progress marker."""
    from src.sources.adapters import recovery_browseruse_refactored as rec_mod
    monkeypatch.setattr(rec_mod, "BrowserUseRecoveryRefactored",
                        lambda *a, **kw: _CountingFakeRecovery(redispatch=False))

    page = _FakePage(url=JOB_URL, submit_click_mode="ok")
    ledger = SubmissionLedger(tmp_path / "ledger.json")
    reg = AtsAdapterRegistry(fallback=GenericAtsAdapter())
    sess = ExternalApplySession(
        {}, registry=reg, ledger=ledger,
        policy=DenyAllPolicy(),   # explicit deny — overrides AutoSubmitPolicy
        run_log=RunLog(agent="test", runs_dir=tmp_path / "runs"),
    )
    sess._closed = False

    async def _start(load_extensions=False, disable_extensions=False):
        return page

    async def _close(save_session=True):
        sess._closed = True

    monkeypatch.setattr(sess, "_start_browser", _start)
    monkeypatch.setattr(sess, "_close_browser", _close)
    prof = tmp_path / "jobright_profile"
    prof.mkdir(exist_ok=True)
    monkeypatch.setattr(type(sess), "_profile_dir", property(lambda self: prof))

    res = await sess.apply(JOB, auto_submit=True)

    assert res.status == "submit_denied_by_policy", (
        f"deny policy must produce submit_denied_by_policy; got {res.status!r}"
    )
    assert page.counter["clicks"] == 0 and page.counter["dispatched"] == 0, (
        f"deny policy must produce zero clicks; got counters={page.counter}"
    )
    # Ledger marker cleared — job stays retryable.
    rec = ledger.record(canonical_key(JOB))
    assert rec is None or rec.get("phase") != PHASE_UNVERIFIED


# --------------------------------------------------------------------------- #
# Scenario F1 — PERSISTENCE proof: fresh Python process reads the same ledger
# file and confirms PHASE_UNVERIFIED survives across process boundary.
#
# This proves the ledger file format is stable across interpreters and that
# needs_reconciliation() (the primitive the session preflight uses) returns
# True on the fresh side. Full session-level enforcement is proved separately
# in Scenario F2 — this test alone is not enough.
# --------------------------------------------------------------------------- #
def test_unverified_ledger_persistence_across_fresh_python_process(tmp_path):
    """Persistence proof only — a fresh Python interpreter can read the
    ledger file and observe PHASE_UNVERIFIED. Does NOT prove session refuses
    to click; that's Scenario F2."""
    key = canonical_key(JOB)
    ledger_path = tmp_path / "ledger.json"
    ledger = SubmissionLedger(ledger_path)
    ledger.complete(key, "attempt-id-from-prior-process", verified=False)
    assert ledger.record(key)["phase"] == PHASE_UNVERIFIED  # sanity

    child_script = textwrap.dedent(f"""
        import json, sys
        from pathlib import Path

        LEDGER = Path({str(ledger_path)!r})
        data = json.loads(LEDGER.read_text())
        rec = data.get({key!r})
        if rec is None:
            print("FAIL: ledger entry missing after restart", file=sys.stderr)
            sys.exit(1)
        if rec.get("phase") != "submission_unverified":
            print(f"FAIL: phase changed across process boundary: {{rec}}", file=sys.stderr)
            sys.exit(1)
        from src.sources.adapters.idempotency import SubmissionLedger
        fresh = SubmissionLedger(LEDGER)
        if not fresh.needs_reconciliation({key!r}):
            print("FAIL: needs_reconciliation returned False in fresh process",
                  file=sys.stderr)
            sys.exit(1)
        print("OK")
    """)

    repo_root = os.fspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child_script],
        capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    assert result.returncode == 0, (
        f"fresh-python subprocess failed to see PHASE_UNVERIFIED:\n"
        f"  stdout: {result.stdout!r}\n  stderr: {result.stderr!r}"
    )
    assert "OK" in result.stdout


# --------------------------------------------------------------------------- #
# Scenario F2 — ENFORCEMENT proof: fresh Python process spawns a real
# ExternalApplySession against the persisted ledger file and confirms the
# session preflight refuses to reach the submit boundary — no browser launch,
# no click dispatch, ledger untouched.
#
# This closes the gap Scenario F1 leaves: persistence alone does not prove
# the session actually enforces reconciliation on restart.
# --------------------------------------------------------------------------- #
def test_unverified_ledger_blocks_session_in_fresh_python_process(tmp_path):
    """Enforcement proof — a fresh Python interpreter constructs a real
    ExternalApplySession pointed at the persisted ledger, calls session.apply,
    and asserts that the session refused to reach the submit boundary."""
    key = canonical_key(JOB)
    ledger_path = tmp_path / "ledger.json"
    ledger = SubmissionLedger(ledger_path)
    ledger.complete(key, "attempt-id-from-prior-process", verified=False)
    assert ledger.record(key)["phase"] == PHASE_UNVERIFIED  # sanity

    prof_dir = tmp_path / "jobright_profile"
    prof_dir.mkdir(exist_ok=True)
    runs_dir = tmp_path / "runs"

    # The child constructs a real session, faking ONLY the browser boundary
    # (start/close + profile dir), runs the same JOB, and prints a JSON
    # result the parent asserts against. If the session's preflight worked,
    # no click ever dispatched — the counter stays at 0.
    child_script = textwrap.dedent(f"""
        import asyncio, json, sys
        from pathlib import Path

        from src.events import RunLog
        from src.sources.adapters.session import ExternalApplySession
        from src.sources.adapters.registry import AtsAdapterRegistry
        from src.sources.adapters.generic import GenericAtsAdapter
        from src.sources.adapters.idempotency import SubmissionLedger

        LEDGER = Path({str(ledger_path)!r})
        PROF = Path({str(prof_dir)!r})
        RUNS = Path({str(runs_dir)!r})
        JOB = {JOB!r}

        # Track every attempted browser-boundary call. If preflight enforcement
        # works, browser_started must stay False.
        events = {{"browser_started": False, "clicks": 0}}

        class Page:
            url = "https://never.reached/"
            frames = []
            async def title(self): return "never"
            async def goto(self, *a, **kw):
                events["browser_started"] = True
                return None
            async def query_selector(self, sel):
                events["clicks"] += 1
                return None
            async def evaluate(self, js, *a):
                return None
            async def set_input_files(self, sel, path):
                return None

        async def go():
            sess = ExternalApplySession(
                {{}}, registry=AtsAdapterRegistry(fallback=GenericAtsAdapter()),
                ledger=SubmissionLedger(LEDGER),
                run_log=RunLog(agent="child", runs_dir=RUNS),
            )
            sess._closed = False
            async def _start(load_extensions=False, disable_extensions=False):
                events["browser_started"] = True
                return Page()
            async def _close(save_session=True):
                sess._closed = True
            sess._start_browser = _start
            sess._close_browser = _close
            type(sess)._profile_dir = property(lambda self: PROF)

            res = await sess.apply(JOB, auto_submit=True)
            return {{
                "status": res.status,
                "browser_started": events["browser_started"],
                "clicks": events["clicks"],
            }}

        out = asyncio.run(go())
        sys.stdout.write(json.dumps(out))
    """)

    repo_root = os.fspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child_script],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    if proc.returncode != 0:
        pytest.fail(
            f"child process failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        pytest.fail(f"child stdout not JSON: {exc}\nstdout={proc.stdout!r}")

    assert out["status"] == "submit_unverified_unresolved", (
        f"fresh session must refuse the resubmit; got status={out['status']!r}"
    )
    assert out["browser_started"] is False, (
        f"session must NOT launch the browser on a reconciliation hold; "
        f"got browser_started={out['browser_started']}"
    )
    assert out["clicks"] == 0, (
        f"no submit clicks may fire; got clicks={out['clicks']}"
    )
    # Ledger must not have been mutated by the refused attempt.
    rec = SubmissionLedger(ledger_path).record(key)
    assert rec is not None and rec.get("phase") == PHASE_UNVERIFIED, (
        f"ledger must remain PHASE_UNVERIFIED after refusal; got {rec!r}"
    )


# --------------------------------------------------------------------------- #
# Scenario G — clean happy path baseline (single dispatch, verified receipt)
# proves the fake fixture reports counts correctly when nothing goes wrong.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_clean_submit_with_receipt_verifies(tmp_path, monkeypatch):
    from src.sources.adapters import recovery_browseruse_refactored as rec_mod
    monkeypatch.setattr(rec_mod, "BrowserUseRecoveryRefactored",
                        lambda *a, **kw: _CountingFakeRecovery(redispatch=False))

    page = _FakePage(url=JOB_URL, submit_click_mode="ok",
                     receipt_signal="t:application submitted")
    sess, ledger = _make_session(tmp_path, page, monkeypatch)

    res = await sess.apply(JOB, auto_submit=True)

    assert res.status == "applied" and res.verified is True
    assert page.counter["dispatched"] == 1
    rec = ledger.record(canonical_key(JOB))
    assert rec is not None and rec.get("phase") == PHASE_VERIFIED
